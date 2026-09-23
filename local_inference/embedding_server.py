"""Authenticated local BGE inference. Model weights never leave this machine."""

import os
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

MODEL_NAME = "bge-small-zh-v1.5"
MODEL_PATH = Path(os.environ.get("LOCAL_EMBEDDING_PATH", "data/local-models/bge-small-zh-v1.5"))
TOKEN = os.environ.get("EMBEDDING_API_KEY", "")
lock = threading.Lock()
model = None


@asynccontextmanager
async def lifespan(app):
    global model
    import torch
    from sentence_transformers import SentenceTransformer

    if not TOKEN:
        raise RuntimeError("EMBEDDING_API_KEY is required, even for the local inference service")
    torch.set_num_threads(4)
    model = SentenceTransformer(
        str(MODEL_PATH.resolve()), device="cpu", local_files_only=True, trust_remote_code=False
    )
    model.max_seq_length = 512
    yield
    model = None


app = FastAPI(title="Local BGE embeddings", lifespan=lifespan, docs_url=None, redoc_url=None)


def authenticate(authorization: str | None = Header(default=None)):
    if authorization is None or not secrets.compare_digest(authorization, f"Bearer {TOKEN}"):
        raise HTTPException(401, "Invalid local inference token")


class EmbeddingRequest(BaseModel):
    model: str
    input: list[str] | str
    dimensions: int = Field(default=512, ge=1)
    encoding_format: str = "float"


@app.get("/health")
def health():
    return {
        "status": "ok" if model is not None else "loading",
        "model": MODEL_NAME,
        "dimensions": 512,
        "device": "cpu",
        "inference": "real_local_model",
    }


@app.post("/v1/embeddings", dependencies=[Depends(authenticate)])
def embeddings(body: EmbeddingRequest):
    if body.model != MODEL_NAME or body.dimensions != 512 or body.encoding_format != "float":
        raise HTTPException(422, "This service supports bge-small-zh-v1.5, 512 dimensions, float output")
    texts = [body.input] if isinstance(body.input, str) else body.input
    if not 1 <= len(texts) <= 32 or any(not text.strip() or len(text) > 3000 for text in texts):
        raise HTTPException(422, "Expected 1-32 nonempty texts, each at most 3000 characters")
    if model is None:
        raise HTTPException(503, "Model is loading")
    with lock:
        # Reject, rather than silently truncate, evidence beyond the model's context.
        tokenized = model.tokenizer(texts, truncation=False, add_special_tokens=True)
        lengths = [len(ids) for ids in tokenized["input_ids"]]
        if any(length > 512 for length in lengths):
            raise HTTPException(
                422, "Text exceeds the embedding token limit; reduce CHUNK_SIZE or question length"
            )
        vectors = model.encode(texts, batch_size=16, normalize_embeddings=True, show_progress_bar=False)
    tokens = sum(lengths)
    return {
        "object": "list",
        "model": MODEL_NAME,
        "data": [
            {"object": "embedding", "index": index, "embedding": vector.tolist()}
            for index, vector in enumerate(vectors)
        ],
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
    }
