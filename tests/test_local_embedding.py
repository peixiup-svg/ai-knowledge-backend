"""HTTP contract and truncation safeguards; real inference is verified separately."""

import pytest
from fastapi.testclient import TestClient

from local_inference import embedding_server as service


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(service, "TOKEN", "test-local-token")
    monkeypatch.setattr(service, "model", None)
    return TestClient(service.app)


def test_embedding_requires_auth(client):
    response = client.post("/v1/embeddings", json={"model": service.MODEL_NAME, "input": "hello"})
    assert response.status_code == 401


@pytest.mark.parametrize(
    "change",
    [
        {"input": " "},
        {"input": []},
        {"input": ["a"] * 33},
        {"dimensions": 384},
        {"model": "wrong"},
        {"encoding_format": "base64"},
    ],
)
def test_embedding_rejects_invalid_contract(client, change):
    body = {"model": service.MODEL_NAME, "input": "hello", **change}
    assert (
        client.post(
            "/v1/embeddings", json=body, headers={"Authorization": "Bearer test-local-token"}
        ).status_code
        == 422
    )


def test_embedding_loading_returns_503(client):
    assert (
        client.post(
            "/v1/embeddings",
            json={"model": service.MODEL_NAME, "input": "hello"},
            headers={"Authorization": "Bearer test-local-token"},
        ).status_code
        == 503
    )


def test_embedding_never_silently_truncates(client, monkeypatch):
    class OversizedModel:
        def tokenizer(self, texts, **kwargs):
            assert kwargs["truncation"] is False
            return {"input_ids": [[0] * 513]}

        def encode(self, *args, **kwargs):
            pytest.fail("Oversized text must be rejected before inference")

    monkeypatch.setattr(service, "model", OversizedModel())
    response = client.post(
        "/v1/embeddings",
        json={"model": service.MODEL_NAME, "input": "hello"},
        headers={"Authorization": "Bearer test-local-token"},
    )
    assert response.status_code == 422
