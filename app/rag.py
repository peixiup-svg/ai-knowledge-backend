"""Page-aware ingestion and scoped retrieval, without a hidden orchestration framework.

The hash embedder is a deterministic lexical baseline for an offline demonstration.
It is not a semantic model and its score is not a probability of correctness.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import httpx
import pymupdf
from pgvector.sqlalchemy import Vector
from sqlalchemy import select, type_coerce
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Document, DocumentChunk


class PermanentDocumentError(Exception):
    """Invalid input or configuration that must not be automatically retried."""


class TransientProviderError(Exception):
    """A bounded worker retry may recover from this upstream failure."""


@dataclass(frozen=True)
class ParsedChunk:
    content: str
    page_number: int
    chunk_index: int


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    document_id: str
    filename: str
    page_number: int
    content: str
    score: float


def _split_page(text: str, size: int, overlap: int) -> list[str]:
    """Prefer paragraph/sentence boundaries without dropping characters between chunks."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").strip()
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # Never let a preferred boundary make the next start stall.
            lower = start + max(size // 2, overlap + 1)
            segment = text[lower:end]
            boundaries = list(re.finditer(r"\n|[。！？]|[.!?](?:\s|$)", segment))
            if boundaries:
                end = lower + boundaries[-1].end()
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= len(text):
            break
        start = end - overlap
    return pieces


def parse_and_chunk(path: Path, filename: str, settings: Settings) -> list[ParsedChunk]:
    """Parse UTF-8 text or a text-layer PDF; preserve one-based PDF page numbers."""
    if path.stat().st_size > settings.max_upload_bytes:
        raise PermanentDocumentError("文件超过上传大小限制。")
    suffix = Path(filename).suffix.lower()
    pages: list[tuple[int, str]] = []
    if suffix == ".pdf":
        try:
            with pymupdf.open(path, filetype="pdf") as pdf:
                if pdf.needs_pass:
                    raise PermanentDocumentError("暂不支持加密 PDF，请上传未加密文件。")
                if pdf.page_count > settings.max_pdf_pages:
                    raise PermanentDocumentError("PDF 页数超过限制。")
                total = 0
                for number, page in enumerate(pdf, start=1):
                    text = page.get_text("text", sort=True)
                    total += len(text)
                    if total > settings.max_document_chars:
                        raise PermanentDocumentError("解析后的文档文本超过长度限制。")
                    pages.append((number, text))
        except PermanentDocumentError:
            raise
        except (pymupdf.FileDataError, RuntimeError, ValueError) as exc:
            raise PermanentDocumentError("无法解析 PDF，请检查文件是否损坏。") from exc
    elif suffix in {".md", ".txt"}:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeError as exc:
            raise PermanentDocumentError("TXT 和 Markdown 文件必须使用 UTF-8 编码。") from exc
        if "\x00" in text:
            raise PermanentDocumentError("文件包含二进制内容，无法作为文本解析。")
        if len(text) > settings.max_document_chars:
            raise PermanentDocumentError("文档文本超过长度限制。")
        pages.append((1, text))
    else:
        raise PermanentDocumentError("仅支持 PDF、Markdown 和 TXT 文件。")

    chunks = [
        ParsedChunk(content=piece, page_number=page_number, chunk_index=index)
        for index, (page_number, piece) in enumerate(
            (page_number, piece)
            for page_number, text in pages
            for piece in _split_page(text, settings.chunk_size, settings.chunk_overlap)
        )
    ]
    if not chunks:
        raise PermanentDocumentError("没有可提取的文字；扫描件需要先进行 OCR。")
    return chunks


def embedding_fingerprint(settings: Settings) -> str:
    """Changing providers, endpoint, model, or dimensions requires re-ingestion."""
    if settings.embedding_provider == "hash":
        return f"hash:han-latin-v1:{settings.embedding_dimensions}"
    configuration = json.dumps(
        {
            "provider": settings.embedding_provider,
            "endpoint": settings.embedding_base_url.rstrip("/"),
            "model": settings.embedding_model,
            "dimensions": settings.embedding_dimensions,
        },
        sort_keys=True,
    )
    return "openai:" + hashlib.sha256(configuration.encode()).hexdigest()


def _validated_unit_vector(value: object, dimensions: int) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != dimensions:
        raise PermanentDocumentError("向量服务返回的维度与 EMBEDDING_DIMENSIONS 不一致。")
    if any(isinstance(x, bool) or not isinstance(x, (float, int)) for x in value):
        raise PermanentDocumentError("向量服务返回了无效数值。")
    try:
        vector = [float(x) for x in value]
    except OverflowError:
        raise PermanentDocumentError("向量服务返回了超出浮点数范围的数值。") from None
    if not all(math.isfinite(x) for x in vector):
        raise PermanentDocumentError("向量服务返回了非有限数值。")
    # Scaling avoids overflow even when a provider returns finite but enormous numbers.
    scale = max(abs(x) for x in vector)
    if scale == 0:
        raise PermanentDocumentError("向量服务返回了全零向量。")
    scaled = [x / scale for x in vector]
    norm = math.sqrt(sum(x * x for x in scaled))
    return [x / norm for x in scaled]


def _hash_embedding(text: str, dimensions: int) -> list[float]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    weighted: Counter[str] = Counter()
    for match in re.finditer(r"[a-z0-9_]+|[\u3400-\u9fff]+", normalized):
        word = match.group()
        if "\u3400" <= word[0] <= "\u9fff":
            for char in word:
                weighted[char] += 0.35
            weighted.update(word[i : i + 2] for i in range(len(word) - 1))
        else:
            weighted[word] += 1
    if not weighted:
        weighted[normalized.strip()] = 1
    vector = [0.0] * dimensions
    for token, count in weighted.items():
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
        position = int.from_bytes(digest[:8], "little") % dimensions
        sign = 1 if digest[8] & 1 else -1
        vector[position] += sign * math.log1p(count)
    if not any(vector):
        # Exact hash cancellation is possible for very small demonstration dimensions.
        vector[int.from_bytes(hashlib.sha256(normalized.encode()).digest()[:4], "little") % dimensions] = 1
    return _validated_unit_vector(vector, dimensions)


def embed_texts(texts: list[str], settings: Settings) -> list[list[float]]:
    """Bound batch sizes and validate upstream ordering, count, dimensions and numbers."""
    if not texts:
        return []
    if any(not isinstance(text, str) or not text.strip() for text in texts):
        raise PermanentDocumentError("无法为缺失或空白文本生成向量。")
    if settings.embedding_provider == "hash":
        return [_hash_embedding(text, settings.embedding_dimensions) for text in texts]

    vectors: list[list[float]] = []
    try:
        with httpx.Client(timeout=httpx.Timeout(settings.llm_timeout_seconds, connect=10)) as client:
            for start in range(0, len(texts), 32):
                batch = texts[start : start + 32]
                response = client.post(
                    settings.embedding_base_url.rstrip("/") + "/embeddings",
                    headers={"Authorization": f"Bearer {settings.embedding_api_key.get_secret_value()}"},
                    json={
                        "model": settings.embedding_model,
                        "input": batch,
                        "dimensions": settings.embedding_dimensions,
                        "encoding_format": "float",
                    },
                )
                if response.status_code == 429 or response.status_code >= 500:
                    raise TransientProviderError("向量服务繁忙或暂不可用，请稍后重试。")
                if response.is_error:
                    raise PermanentDocumentError("向量服务拒绝请求，请检查模型、密钥和维度配置。")
                try:
                    payload = response.json()
                    data = payload["data"]
                    if not isinstance(data, list) or len(data) != len(batch):
                        raise ValueError("wrong result count")
                    indexed: dict[int, list[float]] = {}
                    for item in data:
                        index = item["index"]
                        if type(index) is not int or not 0 <= index < len(batch) or index in indexed:
                            raise ValueError("invalid or duplicate index")
                        indexed[index] = _validated_unit_vector(
                            item["embedding"], settings.embedding_dimensions
                        )
                    vectors.extend(indexed[index] for index in range(len(batch)))
                except (KeyError, TypeError, ValueError):
                    raise PermanentDocumentError("向量服务返回的数据格式无效。") from None
    except httpx.RequestError:
        # Provider exception strings may contain URLs, credentials, or response fragments.
        raise TransientProviderError("连接向量服务失败或超时，请稍后重试。") from None
    return vectors


def retrieve(
    session: Session, knowledge_base_id: str, query: str, settings: Settings
) -> list[RetrievedChunk]:
    """The API must check KB ownership before calling this scoped SQL query."""
    query_vector = embed_texts([query], settings)[0]
    filters = (
        DocumentChunk.knowledge_base_id == knowledge_base_id,
        Document.knowledge_base_id == knowledge_base_id,
        Document.status == "ready",
        Document.deleted_at.is_(None),
        Document.embedding_fingerprint == embedding_fingerprint(settings),
        DocumentChunk.version == Document.version,
    )
    base = select(DocumentChunk, Document.filename).join(Document).where(*filters)
    result: list[tuple[DocumentChunk, str, float]]
    if session.get_bind().dialect.name == "postgresql":
        # The mapped Python-side base type is JSON; explicitly select the pgvector comparator.
        distance = type_coerce(
            DocumentChunk.embedding, Vector(settings.embedding_dimensions)
        ).cosine_distance(query_vector)
        statement = base.add_columns(distance.label("distance")).order_by(distance, DocumentChunk.id)
        rows = session.execute(statement.limit(settings.retrieval_top_k))
        result = [(chunk, filename, 1.0 - float(distance)) for chunk, filename, distance in rows]
    else:
        # SQLite is for small local demos: stream candidates and retain only top-k in memory.
        heap: list[tuple[float, str, DocumentChunk, str]] = []
        for chunk, filename in session.execute(base.execution_options(yield_per=200)):
            vector = _validated_unit_vector(chunk.embedding, settings.embedding_dimensions)
            score = sum(left * right for left, right in zip(query_vector, vector, strict=True))
            entry = (score, chunk.id, chunk, filename)
            if len(heap) < settings.retrieval_top_k:
                heapq.heappush(heap, entry)
            elif entry[:2] > heap[0][:2]:
                heapq.heapreplace(heap, entry)
        result = [(chunk, filename, score) for score, _, chunk, filename in sorted(heap, reverse=True)]
    return [
        RetrievedChunk(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            filename=filename,
            page_number=chunk.page_number,
            content=chunk.content,
            score=max(-1.0, min(1.0, score)),
        )
        for chunk, filename, score in result
        if math.isfinite(score) and score >= settings.retrieval_min_score
    ]


def build_citations(chunks: list[RetrievedChunk]) -> list[dict]:
    return [
        {
            "index": index,
            "chunk_id": chunk.chunk_id,
            "document_id": chunk.document_id,
            "filename": chunk.filename,
            "page_number": chunk.page_number,
            "content": chunk.content,
            "score": round(chunk.score, 6),
        }
        for index, chunk in enumerate(chunks, start=1)
    ]
