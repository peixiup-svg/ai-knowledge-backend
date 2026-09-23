"""Public API checks for ownership, deletion, failure handling, and chat persistence."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main
from app.config import get_settings
from app.db import SessionLocal
from app.ingestion import process_document
from app.limiting import limiter
from app.models import User
from app.providers import GenerationError
from app.schemas import ChatIn


def parse_sse(response):
    events = []
    for block in response.text.replace("\r\n", "\n").split("\n\n"):
        event_name = "message"
        data_lines = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines:
            events.append((event_name, json.loads("\n".join(data_lines))))
    return events


def test_authentication_and_token_tampering(client, account):
    headers = account()
    me = client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["email"] == "alice@example.com"
    assert "password_hash" not in me.text
    assert client.get("/api/v1/knowledge-bases").status_code == 401
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer invalid"}).status_code == 401
    token = headers["Authorization"].split()[1]
    parts = token.split(".")
    parts[1] = "eyJzdWIiOiJpbXBvc3RvciJ9"
    assert (
        client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + ".".join(parts)}).status_code
        == 401
    )
    invalid = client.post(
        "/api/v1/auth/login", json={"email": "alice@example.com", "password": "Wrong-password!"}
    )
    unknown = client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "Wrong-password!"}
    )
    assert invalid.status_code == unknown.status_code == 401
    assert invalid.json() == unknown.json()
    assert (
        client.post(
            "/api/v1/auth/register", json={"email": "alice@example.com", "password": "Another-password!"}
        ).status_code
        == 409
    )


def test_password_and_knowledge_base_validation(client, headers):
    assert (
        client.post(
            "/api/v1/auth/register", json={"email": "weak@example.com", "password": "short"}
        ).status_code
        == 422
    )
    assert client.post("/api/v1/knowledge-bases", headers=headers, json={"name": ""}).status_code == 422


def test_all_resource_entry_points_enforce_ownership(
    client, account, headers, knowledge_base, ready_document
):
    kb_id = knowledge_base["id"]
    document_id, job_id = ready_document["document_id"], ready_document["job_id"]
    chat = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/chat",
        headers=headers,
        json={"question": "What is the annual leave policy?"},
    )
    conversation_id = dict(parse_sse(chat))["meta"]["conversation_id"]
    other = account("bob@example.com")
    assert client.get("/api/v1/knowledge-bases", headers=other).json() == []
    other_kb = client.post("/api/v1/knowledge-bases", headers=other, json={"name": "Bob KB"}).json()["id"]
    cases = [
        ("GET", f"/knowledge-bases/{kb_id}", {}),
        ("PATCH", f"/knowledge-bases/{kb_id}", {"json": {"name": "stolen"}}),
        ("DELETE", f"/knowledge-bases/{kb_id}", {}),
        ("GET", f"/knowledge-bases/{kb_id}/documents", {}),
        (
            "POST",
            f"/knowledge-bases/{kb_id}/documents",
            {"files": {"file": ("x.txt", b"new document", "text/plain")}},
        ),
        ("POST", f"/knowledge-bases/{kb_id}/search", {"json": {"question": "annual leave"}}),
        ("POST", f"/knowledge-bases/{kb_id}/chat", {"json": {"question": "annual leave"}}),
        ("GET", f"/documents/{document_id}", {}),
        ("GET", f"/documents/{document_id}/download", {}),
        ("DELETE", f"/documents/{document_id}", {}),
        ("GET", f"/jobs/{job_id}", {}),
        ("POST", f"/jobs/{job_id}/retry", {}),
        ("GET", f"/conversations/{conversation_id}/messages", {}),
        ("GET", f"/conversations?knowledge_base_id={kb_id}", {}),
        (
            "POST",
            f"/knowledge-bases/{other_kb}/chat",
            {"json": {"question": "annual leave", "conversation_id": conversation_id}},
        ),
    ]
    for method, path, kwargs in cases:
        response = client.request(method, "/api/v1" + path, headers=other, **kwargs)
        assert response.status_code == 404, (method, path, response.status_code, response.text)
    assert client.get("/api/v1/usage", headers=other).json()["calls"] == 0
    assert client.get("/api/v1/usage", headers=headers).json()["calls"] == 1
    assert client.get(f"/api/v1/documents/{document_id}", headers=headers).status_code == 200


def test_duplicate_upload_is_per_knowledge_base(client, headers, knowledge_base, uploaded_document):
    def upload(kb_id):
        return client.post(
            f"/api/v1/knowledge-bases/{kb_id}/documents",
            headers=headers,
            files={
                "file": (
                    "renamed.txt",
                    b"Annual leave policy: employees receive 15 days of annual leave.",
                    "text/plain",
                )
            },
        )

    duplicate = upload(knowledge_base["id"])
    assert duplicate.status_code == 202
    assert duplicate.json()["deduplicated"] is True
    assert duplicate.json()["document_id"] == uploaded_document["document_id"]
    assert duplicate.json()["job_id"] == uploaded_document["job_id"]
    second_kb = client.post("/api/v1/knowledge-bases", headers=headers, json={"name": "Separate KB"}).json()[
        "id"
    ]
    separate = upload(second_kb)
    assert separate.status_code == 202
    assert separate.json()["deduplicated"] is False
    assert separate.json()["document_id"] != uploaded_document["document_id"]


def test_document_delete_removes_search_and_download_access(client, headers, knowledge_base, ready_document):
    kb_id, document_id = knowledge_base["id"], ready_document["document_id"]
    search_url = f"/api/v1/knowledge-bases/{kb_id}/search"
    before = client.post(search_url, headers=headers, json={"question": "annual leave policy"})
    assert before.status_code == 200
    assert document_id in {item["document_id"] for item in before.json()["citations"]}
    download = client.get(f"/api/v1/documents/{document_id}/download", headers=headers)
    assert download.status_code == 200
    assert b"15 days" in download.content
    assert client.delete(f"/api/v1/documents/{document_id}", headers=headers).status_code == 204
    assert (
        client.post(search_url, headers=headers, json={"question": "annual leave policy"}).json()["citations"]
        == []
    )
    assert client.get(f"/api/v1/documents/{document_id}/download", headers=headers).status_code == 404
    assert client.get(f"/api/v1/knowledge-bases/{kb_id}/documents", headers=headers).json() == []


def test_deleted_document_can_be_reuploaded_as_new_version(client, headers, knowledge_base, ready_document):
    document_id = ready_document["document_id"]
    assert client.delete(f"/api/v1/documents/{document_id}", headers=headers).status_code == 204
    response = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/documents",
        headers=headers,
        files={
            "file": (
                "policy.txt",
                b"Annual leave policy: employees receive 15 days of annual leave.",
                "text/plain",
            )
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["document_id"] == document_id
    process_document(response.json()["job_id"])
    document = client.get(f"/api/v1/documents/{document_id}", headers=headers).json()
    assert document["version"] == 2
    assert document["status"] == "ready"


def test_unsupported_and_oversized_uploads_rejected(client, headers, knowledge_base, monkeypatch):
    url = f"/api/v1/knowledge-bases/{knowledge_base['id']}/documents"
    unsupported = client.post(
        url, headers=headers, files={"file": ("program.exe", b"MZ", "application/octet-stream")}
    )
    assert unsupported.status_code in (400, 415)
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 1024)
    oversized = client.post(url, headers=headers, files={"file": ("too-big.txt", b"x" * 1025, "text/plain")})
    assert oversized.status_code == 413
    assert client.get(url, headers=headers).json() == []


def test_corrupt_pdf_fails_as_a_sanitized_job(client, headers, knowledge_base):
    response = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/documents",
        headers=headers,
        files={"file": ("broken.pdf", b"%PDF-1.7\nThis is not a valid PDF file.", "application/pdf")},
    )
    assert response.status_code == 202, response.text
    process_document(response.json()["job_id"])
    job = client.get(f"/api/v1/jobs/{response.json()['job_id']}", headers=headers).json()
    assert job["status"] == "failed"
    assert job["error"]
    assert str(get_settings().storage_dir) not in str(job)
    assert "Traceback" not in str(job)


def test_chat_sse_and_saved_messages_agree(client, headers, knowledge_base, ready_document):
    response = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/chat",
        headers=headers,
        json={"question": "How many days of annual leave do employees receive?"},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers.get("x-request-id")
    events = parse_sse(response)
    assert events[0][0] == "meta"
    assert events[-1][0] == "done"
    meta = next(data for event, data in events if event == "meta")
    sources = next(data for event, data in events if event == "sources")["citations"]
    done = events[-1][1]
    answer = "".join(data["content"] for event, data in events if event == "token")
    assert meta["provider"] == "demo"
    assert sources and sources[0]["document_id"] == ready_document["document_id"]
    assert answer and done["status"] == "completed"
    messages = client.get(f"/api/v1/conversations/{meta['conversation_id']}/messages", headers=headers).json()
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[-1]["id"] == done["message_id"]
    assert messages[-1]["content"] == answer
    assert messages[-1]["citations"] == sources
    assert messages[-1]["status"] == "completed"
    usage = client.get("/api/v1/usage", headers=headers).json()
    assert usage["calls"] == usage["successful_calls"] == 1
    assert usage["usage_unavailable_calls"] == 1


def test_empty_knowledge_base_produces_refusal_without_sources(client, headers, knowledge_base):
    response = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/chat",
        headers=headers,
        json={"question": "How much is the travel budget?"},
    )
    assert response.status_code == 200
    events = parse_sse(response)
    assert next(data for event, data in events if event == "sources")["citations"] == []
    assert events[-1][0] == "done"
    assert "".join(data["content"] for event, data in events if event == "token")


def test_search_request_validation(client, headers, knowledge_base):
    url = f"/api/v1/knowledge-bases/{knowledge_base['id']}/search"
    assert client.post(url, headers=headers, json={"question": ""}).status_code == 422
    assert client.post(url, headers=headers, json={"question": "leave", "top_k": 1000}).status_code == 422


def test_deleting_knowledge_base_hides_all_children(client, headers, knowledge_base, ready_document):
    kb_id = knowledge_base["id"]
    chat = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/chat", headers=headers, json={"question": "annual leave policy"}
    )
    conversation_id = next(data["conversation_id"] for event, data in parse_sse(chat) if event == "meta")
    assert client.delete(f"/api/v1/knowledge-bases/{kb_id}", headers=headers).status_code == 204
    assert client.get("/api/v1/knowledge-bases", headers=headers).json() == []
    for path in (
        f"/knowledge-bases/{kb_id}",
        f"/knowledge-bases/{kb_id}/documents",
        f"/documents/{ready_document['document_id']}",
        f"/documents/{ready_document['document_id']}/download",
        f"/jobs/{ready_document['job_id']}",
        f"/conversations/{conversation_id}/messages",
        f"/conversations?knowledge_base_id={kb_id}",
    ):
        assert client.get("/api/v1" + path, headers=headers).status_code == 404, path
    upload = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        headers=headers,
        files={"file": ("new.txt", b"should not store", "text/plain")},
    )
    assert upload.status_code == 404


def test_partial_generation_failure_is_saved_as_failed(
    client, headers, knowledge_base, ready_document, monkeypatch
):
    async def interrupted_stream(*args, **kwargs):
        yield {"type": "token", "content": "Partial answer"}
        raise GenerationError("upstream_timeout", "模型响应超时，回答可能不完整。")

    monkeypatch.setattr(main, "stream_answer", interrupted_stream)
    response = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/chat",
        headers=headers,
        json={"question": "annual leave policy"},
    )
    assert response.status_code == 200
    events = parse_sse(response)
    assert any(event == "error" and data["code"] == "upstream_timeout" for event, data in events)
    assert not any(event == "done" and data.get("status") == "completed" for event, data in events)
    conversation_id = next(data["conversation_id"] for event, data in events if event == "meta")
    messages = client.get(f"/api/v1/conversations/{conversation_id}/messages", headers=headers).json()
    assert messages[-1]["status"] == "failed"
    assert messages[-1]["content"] == "Partial answer"
    usage = client.get("/api/v1/usage", headers=headers).json()
    assert usage["calls"] == usage["failed_calls"] == 1
    assert usage["successful_calls"] == 0


def test_disconnect_immediately_closes_provider_and_saves_cancelled(
    client, headers, knowledge_base, ready_document, monkeypatch
):
    closed = []

    async def fake_provider(*args, **kwargs):
        try:
            yield {"type": "token", "content": "Should not reach disconnected client"}
        finally:
            closed.append(True)

    async def disconnected():
        return True

    monkeypatch.setattr(main, "stream_answer", fake_provider)
    with SessionLocal() as session:
        user_id = session.scalar(select(User.id))

    async def drive_response():
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(generation_slots=asyncio.Semaphore(1))),
            is_disconnected=disconnected,
        )
        response = await main.chat(
            knowledge_base["id"], ChatIn(question="annual leave policy"), request, SimpleNamespace(id=user_id)
        )
        blocks = [block async for block in response.body_iterator]
        # Must close before response finishes, not later during event-loop GC.
        assert closed == [True]
        assert request.app.state.generation_slots._value == 1
        return "".join(blocks)

    events = parse_sse(SimpleNamespace(text=asyncio.run(drive_response())))
    assert not any(event in {"token", "done"} for event, _ in events)
    conversation_id = next(data["conversation_id"] for event, data in events if event == "meta")
    messages = client.get(f"/api/v1/conversations/{conversation_id}/messages", headers=headers).json()
    assert messages[-1]["status"] == "cancelled"
    assert messages[-1]["content"] == ""


def test_internal_error_is_sanitized_and_has_request_id(client, monkeypatch):
    def failing_hash(*args, **kwargs):
        raise RuntimeError("secret-database-url secret-password")

    monkeypatch.setattr(main, "hash_password", failing_hash)
    with TestClient(main.app, raise_server_exceptions=False) as guarded_client:
        response = guarded_client.post(
            "/api/v1/auth/register", json={"email": "safe@example.com", "password": "Safe-test-password!"}
        )
    assert response.status_code == 500
    assert response.headers.get("x-request-id")
    assert "secret-database-url" not in response.text
    assert "secret-password" not in response.text


def test_request_body_limit_handles_declared_and_streamed_bodies(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 1024)
    oversized = b"x" * (1024 + 65536 + 1)
    declared = client.post(
        "/api/v1/auth/register", content=oversized, headers={"Content-Type": "application/json"}
    )
    assert declared.status_code == 413

    def body_chunks():
        yield oversized[:40000]
        yield oversized[40000:]

    streamed = client.post(
        "/api/v1/auth/register", content=body_chunks(), headers={"Content-Type": "application/json"}
    )
    assert streamed.status_code == 413


def test_chat_rate_limit_rejects_before_second_call(client, headers, knowledge_base, monkeypatch):
    limiter.reset()
    monkeypatch.setattr(get_settings(), "chat_requests_per_minute", 1)
    url = f"/api/v1/knowledge-bases/{knowledge_base['id']}/chat"
    assert client.post(url, headers=headers, json={"question": "annual leave"}).status_code == 200
    rejected = client.post(url, headers=headers, json={"question": "annual leave"})
    assert rejected.status_code == 429
    assert 1 <= int(rejected.headers["retry-after"]) <= 60
    assert client.get("/api/v1/usage", headers=headers).json()["calls"] == 1


def test_failed_job_can_be_retried_but_successful_job_cannot(client, headers, uploaded_document, monkeypatch):
    from app import ingestion
    from app.rag import PermanentDocumentError

    original_parse = ingestion.parse_and_chunk

    def unavailable_parser(*args, **kwargs):
        raise PermanentDocumentError("模拟解析失败。")

    monkeypatch.setattr(ingestion, "parse_and_chunk", unavailable_parser)
    assert process_document(uploaded_document["job_id"]) == "failed"
    retry_url = f"/api/v1/jobs/{uploaded_document['job_id']}/retry"
    response = client.post(retry_url, headers=headers)
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "queued"
    monkeypatch.setattr(ingestion, "parse_and_chunk", original_parse)
    assert process_document(uploaded_document["job_id"]) == "succeeded"
    assert client.post(retry_url, headers=headers).status_code == 409


def test_evaluation_ignores_user_database_and_paid_provider_environment(tmp_path):
    """Running the documented command must never use a configured paid API or DB."""
    repository = Path(__file__).resolve().parents[1]
    sentinel = tmp_path / "users-real-database.db"
    sentinel.write_bytes(b"Do not connect to or modify this existing user data")
    original = sentinel.read_bytes()
    report_path = tmp_path / "evaluation.json"
    environment = dict(os.environ)
    environment.update(
        APP_ENV="production",
        DATABASE_URL=f"sqlite:///{sentinel.as_posix()}",
        LLM_PROVIDER="openai",
        LLM_API_KEY="DO-NOT-SEND-THIS-KEY",
        EMBEDDING_PROVIDER="openai",
        EMBEDDING_API_KEY="DO-NOT-SEND-THIS-KEY",
        LLM_BASE_URL="http://127.0.0.1:1",
        EMBEDDING_BASE_URL="http://127.0.0.1:1",
        EMBEDDING_DIMENSIONS="16",
        CHUNK_SIZE="10",
        AUTO_CREATE_SCHEMA="false",
    )
    completed = subprocess.run(
        [sys.executable, str(repository / "scripts/evaluate.py"), "--output", str(report_path)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert sentinel.read_bytes() == original
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["mode"] == "OFFLINE_HASH_DEMO_BASELINE"
    assert report["external_model_calls"] == 0
    assert report["embedding_provider"] == "hash" and report["llm_provider"] == "demo"
    assert report["configuration"]["embedding_dimensions"] == 256
    assert report["metrics"]["questions"] == len(report["results"]) == 24
    assert report["metrics"]["llm_answer_accuracy"] is None
    assert set(report["by_split"]) == {"dev", "test"}
    assert "DO-NOT-SEND-THIS-KEY" not in report_path.read_text(encoding="utf-8")
