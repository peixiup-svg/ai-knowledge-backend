"""Verify parsing limits, vector contracts and retrieval isolation without network calls."""

import math
from types import SimpleNamespace

import httpx
import pymupdf
import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app import rag
from app.config import Settings
from app.models import Base, Document, DocumentChunk, KnowledgeBase, User, utcnow


def configuration(**overrides):
    values = {
        "app_env": "test",
        "llm_provider": "demo",
        "embedding_provider": "hash",
        "embedding_dimensions": 32,
        "chunk_size": 100,
        "chunk_overlap": 20,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def embedding_service(monkeypatch, handler):
    original = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(rag.httpx, "Client", lambda **kwargs: original(transport=transport, **kwargs))
    return configuration(embedding_provider="openai", embedding_api_key="sk-test-secret")


def test_utf8_bom_and_global_chunk_indices(tmp_path):
    path = tmp_path / "guide.md"
    path.write_text("数据库连接配置。" * 50, encoding="utf-8-sig")
    chunks = rag.parse_and_chunk(path, path.name, configuration())
    assert len(chunks) > 1
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.page_number == 1 and 0 < len(chunk.content) <= 100 for chunk in chunks)
    assert "\ufeff" not in chunks[0].content


def test_chunk_overlap_preserves_text_and_chinese_sentence_boundaries():
    text = "甲" * 70 + "。" + "乙" * 100
    pieces = rag._split_page(text, size=100, overlap=20)
    assert pieces[0] == text[:71]
    assert pieces[1][:20] == pieces[0][-20:]
    assert "".join([pieces[0], *[piece[20:] for piece in pieces[1:]]]) == text
    assert "".join(rag._split_page(text, size=100, overlap=0)) == text


def test_pdf_preserves_one_based_page_numbers(tmp_path):
    path = tmp_path / "manual.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "First page: database connection settings.")
        pdf.new_page().insert_text((72, 72), "Second page: deployment instructions.")
        pdf.save(path)
    chunks = rag.parse_and_chunk(path, path.name, configuration())
    assert [chunk.page_number for chunk in chunks] == [1, 2]
    assert [chunk.chunk_index for chunk in chunks] == [0, 1]
    assert "deployment" in chunks[1].content
    with pytest.raises(rag.PermanentDocumentError, match="页数"):
        rag.parse_and_chunk(path, path.name, configuration(max_pdf_pages=1))


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("empty.txt", b" \r\n\t", "没有可提取"),
        ("binary.txt", b"abc\x00def", "二进制"),
        ("encoding.md", b"\xff\xfe\x80", "UTF-8"),
        ("bad.pdf", b"not a PDF", "无法解析"),
        ("other.exe", b"plain text", "仅支持"),
    ],
)
def test_invalid_documents_fail_permanently(tmp_path, filename, content, message):
    path = tmp_path / filename
    path.write_bytes(content)
    with pytest.raises(rag.PermanentDocumentError, match=message):
        rag.parse_and_chunk(path, filename, configuration())


def test_document_byte_and_character_limits(tmp_path):
    path = tmp_path / "large.txt"
    path.write_text("甲" * 1001, encoding="utf-8")
    with pytest.raises(rag.PermanentDocumentError, match="大小"):
        rag.parse_and_chunk(path, path.name, configuration(max_upload_bytes=1024))
    with pytest.raises(rag.PermanentDocumentError, match="长度"):
        rag.parse_and_chunk(path, path.name, configuration(max_document_chars=1000))


def test_blank_and_encrypted_pdfs_are_not_silently_accepted(tmp_path):
    blank = tmp_path / "blank.pdf"
    encrypted = tmp_path / "encrypted.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.save(blank)
        pdf.save(encrypted, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
    with pytest.raises(rag.PermanentDocumentError, match="OCR"):
        rag.parse_and_chunk(blank, blank.name, configuration())
    with pytest.raises(rag.PermanentDocumentError, match="加密"):
        rag.parse_and_chunk(encrypted, encrypted.name, configuration())


def test_hash_baseline_is_deterministic_normalized_and_language_aware():
    settings = configuration()
    vectors = rag.embed_texts(["Database DATABASE", "database database", "数据库连接", "!!!"], settings)
    assert vectors[0] == vectors[1]
    assert vectors[0] != vectors[2]
    assert vectors == rag.embed_texts(
        ["Database DATABASE", "database database", "数据库连接", "!!!"], settings
    )
    assert all(len(vector) == 32 and math.isclose(sum(x * x for x in vector), 1) for vector in vectors)
    assert rag.embed_texts([], settings) == []
    with pytest.raises(rag.PermanentDocumentError):
        rag.embed_texts([" "], settings)


@pytest.mark.parametrize(
    "vector",
    [[0] * 8, [1] * 7, [True] * 8, ["1"] * 8, [float("nan")] * 8, [float("inf")] * 8, [10**1000] * 8],
)
def test_embedding_rejects_invalid_numbers_and_dimensions(vector):
    with pytest.raises(rag.PermanentDocumentError):
        rag._validated_unit_vector(vector, 8)


def test_embedding_normalization_avoids_float_overflow():
    vector = rag._validated_unit_vector([1e308, -1e308] + [0] * 6, 8)
    assert all(math.isfinite(value) for value in vector)
    assert math.isclose(sum(value * value for value in vector), 1)


def test_embedding_fingerprint_tracks_model_endpoint_and_dimensions_but_not_secret():
    settings = configuration(embedding_provider="openai", embedding_api_key="first-secret")
    original = rag.embedding_fingerprint(settings)
    assert original == rag.embedding_fingerprint(settings.model_copy(update={"embedding_api_key": "other"}))
    assert original == rag.embedding_fingerprint(
        settings.model_copy(update={"embedding_base_url": settings.embedding_base_url + "/"})
    )
    for field, value in [
        ("embedding_model", "new-model"),
        ("embedding_dimensions", 64),
        ("embedding_base_url", "https://provider.example/v1"),
    ]:
        assert original != rag.embedding_fingerprint(settings.model_copy(update={field: value}))
    assert "secret" not in original


def test_embedding_batches_reorders_and_validates_provider_results(monkeypatch):
    seen_batches = []

    def handler(request):
        import json

        body = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer sk-test-secret"
        assert request.url.path == "/v1/embeddings"
        seen_batches.append(body["input"])
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [index + 1, 1] + [0] * 30}
                    for index in reversed(range(len(body["input"])))
                ]
            },
        )

    settings = embedding_service(monkeypatch, handler)
    vectors = rag.embed_texts([f"document {index}" for index in range(35)], settings)
    assert list(map(len, seen_batches)) == [32, 3]
    assert len(vectors) == 35
    assert math.isclose(vectors[0][0], vectors[0][1])
    assert vectors[1][0] > vectors[1][1]


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        [],
        [{"index": 0, "embedding": [1] * 31}],
        [{"index": True, "embedding": [1] * 32}],
        [{"index": 2, "embedding": [1] * 32}],
    ],
)
def test_embedding_invalid_payload_is_permanent(monkeypatch, data):
    settings = embedding_service(monkeypatch, lambda request: httpx.Response(200, json={"data": data}))
    with pytest.raises(rag.PermanentDocumentError):
        rag.embed_texts(["query"], settings)


def test_embedding_duplicate_indices_are_rejected(monkeypatch):
    settings = embedding_service(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [1] * 32}, {"index": 0, "embedding": [1] * 32}]}
        ),
    )
    with pytest.raises(rag.PermanentDocumentError):
        rag.embed_texts(["one", "two"], settings)


@pytest.mark.parametrize(
    "status,exception",
    [
        (401, rag.PermanentDocumentError),
        (400, rag.PermanentDocumentError),
        (429, rag.TransientProviderError),
        (503, rag.TransientProviderError),
    ],
)
def test_embedding_errors_do_not_expose_provider_body(monkeypatch, status, exception):
    settings = embedding_service(monkeypatch, lambda request: httpx.Response(status, text="sk-test-secret"))
    with pytest.raises(exception) as caught:
        rag.embed_texts(["query"], settings)
    assert "sk-test-secret" not in str(caught.value)


def test_embedding_timeout_is_transient_and_sanitized(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("sk-test-secret", request=request)

    settings = embedding_service(monkeypatch, handler)
    with pytest.raises(rag.TransientProviderError) as caught:
        rag.embed_texts(["query"], settings)
    assert "sk-test-secret" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.fixture
def retrieval_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(id="owner", email="rag@example.com", password_hash="unused"))
        session.add_all(
            [
                KnowledgeBase(id="kb", owner_id="owner", name="one"),
                KnowledgeBase(id="other", owner_id="owner", name="two"),
            ]
        )
        session.commit()
        yield session
    engine.dispose()


def add_chunk(session, settings, identifier, **overrides):
    values = {
        "id": identifier,
        "knowledge_base_id": "kb",
        "filename": identifier + ".txt",
        "storage_path": "unused",
        "sha256": identifier,
        "size_bytes": 1,
        "status": "ready",
        "version": 1,
        "embedding_fingerprint": rag.embedding_fingerprint(settings),
    }
    chunk_kb = overrides.pop("chunk_kb", "kb")
    chunk_version = overrides.pop("chunk_version", 1)
    vector = overrides.pop("vector", rag.embed_texts(["database configuration"], settings)[0])
    values.update(overrides)
    session.add(Document(**values))
    session.add(
        DocumentChunk(
            id="chunk-" + identifier,
            document_id=identifier,
            knowledge_base_id=chunk_kb,
            version=chunk_version,
            chunk_index=0,
            page_number=2,
            content="database configuration",
            embedding=vector,
        )
    )
    session.commit()


def test_retrieval_excludes_other_kbs_deleted_processing_old_versions_and_wrong_models(retrieval_session):
    settings = configuration()
    add_chunk(retrieval_session, settings, "allowed")
    add_chunk(retrieval_session, settings, "processing", status="processing")
    add_chunk(retrieval_session, settings, "deleted", deleted_at=utcnow())
    add_chunk(retrieval_session, settings, "old-version", version=2)
    add_chunk(retrieval_session, settings, "old-model", embedding_fingerprint="different")
    add_chunk(retrieval_session, settings, "other-doc-kb", knowledge_base_id="other")
    add_chunk(retrieval_session, settings, "other-chunk-kb", chunk_kb="other")
    chunks = rag.retrieve(retrieval_session, "kb", "database configuration", settings)
    assert [chunk.document_id for chunk in chunks] == ["allowed"]
    assert rag.build_citations(chunks)[0] == {
        "index": 1,
        "chunk_id": "chunk-allowed",
        "document_id": "allowed",
        "filename": "allowed.txt",
        "page_number": 2,
        "content": "database configuration",
        "score": 1.0,
    }
    assert rag.retrieve(retrieval_session, "absent", "database configuration", settings) == []


def test_retrieval_top_k_and_minimum_score(retrieval_session, monkeypatch):
    settings = configuration(retrieval_top_k=2, retrieval_min_score=0.5)
    monkeypatch.setattr(rag, "embed_texts", lambda texts, settings: [[1.0] + [0.0] * 31 for text in texts])
    for identifier, vector in [
        ("best", [1, 0]),
        ("second", [0.8, 0.6]),
        ("third", [0.6, 0.8]),
        ("irrelevant", [0, 1]),
    ]:
        add_chunk(retrieval_session, settings, identifier, vector=vector + [0] * 30)
    chunks = rag.retrieve(retrieval_session, "kb", "query", settings)
    assert [chunk.document_id for chunk in chunks] == ["best", "second"]
    assert [
        chunk.document_id
        for chunk in rag.retrieve(
            retrieval_session, "kb", "query", settings.model_copy(update={"retrieval_top_k": 20})
        )
    ] == ["best", "second", "third"]


def test_postgres_query_compiles_to_pgvector_distance_with_all_scope_filters():
    statements = []

    class RecordingSession:
        def get_bind(self):
            return SimpleNamespace(dialect=postgresql.dialect())

        def execute(self, statement):
            statements.append(statement)
            return []

    settings = configuration()
    assert rag.retrieve(RecordingSession(), "kb-private", "query", settings) == []
    compiled = statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "document_chunks.embedding <=>" in sql
    assert "documents.status =" in sql and "documents.deleted_at IS NULL" in sql
    assert "documents.embedding_fingerprint =" in sql
    assert "document_chunks.version = documents.version" in sql
    assert "document_chunks.knowledge_base_id =" in sql and "documents.knowledge_base_id =" in sql
    assert "LIMIT" in sql and list(compiled.params.values()).count("kb-private") == 2
