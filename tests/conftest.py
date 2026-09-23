"""Every integration test uses a disposable database and synchronous task dispatch."""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_test_directory = tempfile.TemporaryDirectory(prefix="knowledge-backend-tests-")
_test_root = Path(_test_directory.name)
os.environ.update(
    APP_ENV="test",
    DATABASE_URL=f"sqlite:///{(_test_root / 'test.db').as_posix()}",
    STORAGE_DIR=str(_test_root / "uploads"),
    JWT_SECRET="integration-test-secret-only-0123456789abcdef",
    TASK_MODE="local",
    RATE_LIMIT_BACKEND="memory",
    LLM_PROVIDER="demo",
    EMBEDDING_PROVIDER="hash",
    AUTH_REQUESTS_PER_MINUTE="10000",
    CHAT_REQUESTS_PER_MINUTE="10000",
    AUTO_CREATE_SCHEMA="true",
)

# Environment must be set before app modules construct Settings or the engine.
from app import main  # noqa: E402
from app.db import engine  # noqa: E402
from app.ingestion import process_document  # noqa: E402
from app.limiting import limiter  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    limiter.reset()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "dispatch_job", lambda job_id: None)
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def account(client):
    def create(email="alice@example.com", password="Test-password-2026!"):
        response = client.post("/api/v1/auth/register", json={"email": email, "password": password})
        assert response.status_code == 201, response.text
        login = client.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert login.status_code == 200, login.text
        return {"Authorization": f"Bearer {login.json()['access_token']}"}

    return create


@pytest.fixture
def headers(account):
    return account()


@pytest.fixture
def knowledge_base(client, headers):
    response = client.post("/api/v1/knowledge-bases", headers=headers, json={"name": "Test knowledge base"})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def uploaded_document(client, headers, knowledge_base):
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
    return response.json()


@pytest.fixture
def ready_document(uploaded_document):
    process_document(uploaded_document["job_id"])
    return uploaded_document


def pytest_sessionfinish(session, exitstatus):
    engine.dispose()
    _test_directory.cleanup()
