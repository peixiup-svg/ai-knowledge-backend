"""Reproducible offline retrieval baseline; never opens the user's configured DB.

Run from the repository: python scripts/evaluate.py --output reports/evaluation.json
This is deliberately not an LLM answer-quality benchmark. The tiny authored set
is useful for regression and learning; build a larger independent set for hiring
claims about semantic retrieval. Test labels are visible and not a blind benchmark.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("reports/evaluation.json"))
    parser.add_argument("--dataset", type=Path, default=PROJECT_ROOT / "examples/evaluation.jsonl")
    parser.add_argument("--split", choices=("all", "dev", "test"), default="all")
    parser.add_argument("--top-k", type=int, choices=range(1, 21), default=5)
    parser.add_argument("--min-score", type=float, default=0.08)
    parser.add_argument("--chunk-size", type=int, default=240)
    parser.add_argument("--chunk-overlap", type=int, default=40)
    return parser.parse_args()


def read_dataset(path: Path) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or case["id"] in seen:
            raise ValueError("Dataset IDs must be unique strings")
        seen.add(case["id"])
        if case.get("split") not in {"dev", "test"}:
            raise ValueError("Each dataset row needs split=dev or test")
        if not isinstance(case.get("question"), str) or not case["question"].strip():
            raise ValueError("Questions must be nonempty strings")
        if type(case.get("answerable")) is not bool:
            raise ValueError("answerable must be a boolean")
        evidence = case.get("evidence")
        if not isinstance(evidence, list) or any(not isinstance(item, str) or not item for item in evidence):
            raise ValueError("evidence must contain nonempty text strings")
        if case["answerable"]:
            source = case.get("source")
            if not isinstance(source, str) or Path(source).name != source or "\\" in source:
                raise ValueError("Sources must be filenames located beside the dataset")
            if not evidence or type(case.get("page")) is not int or case["page"] < 1:
                raise ValueError("Answerable rows need evidence and a one-based page")
            if not (path.parent / source).is_file():
                raise ValueError(f"Missing corpus file: {source}")
        elif case.get("source") is not None or case.get("page") is not None or evidence:
            raise ValueError("Unanswerable rows must have null source/page and empty evidence")
    if not cases:
        raise ValueError("Dataset cannot be empty")
    return cases


def configure_isolated_environment(root: Path, args) -> None:
    # Override every Settings field, not just DATABASE_URL, so an existing .env
    # cannot cause network traffic or silently change the benchmark configuration.
    from app.config import Settings

    # Reading .env is disabled; model field defaults do not read environment values.
    values = {name: field.default for name, field in Settings.model_fields.items()}
    for name, value in values.items():
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
        os.environ[name.upper()] = str(value).lower() if isinstance(value, bool) else str(value)
    os.environ.update(
        APP_ENV="test",
        DATABASE_URL=f"sqlite:///{(root / 'evaluation.db').as_posix()}",
        STORAGE_DIR=str(root / "uploads"),
        JWT_SECRET="offline-evaluation-only-0123456789abcdef",
        TASK_MODE="local",
        RATE_LIMIT_BACKEND="memory",
        EMBEDDING_PROVIDER="hash",
        LLM_PROVIDER="demo",
        EMBEDDING_API_KEY="",
        LLM_API_KEY="",
        RETRIEVAL_TOP_K=str(args.top_k),
        RETRIEVAL_MIN_SCORE=str(args.min_score),
        CHUNK_SIZE=str(args.chunk_size),
        CHUNK_OVERLAP=str(args.chunk_overlap),
        AUTO_CREATE_SCHEMA="true",
    )


def ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def aggregate(results: list[dict]) -> dict:
    answerable = [case for case in results if case["answerable"]]
    unanswerable = [case for case in results if not case["answerable"]]
    return {
        "questions": len(results),
        "answerable_questions": len(answerable),
        "unanswerable_questions": len(unanswerable),
        "evidence_hit_at_k": ratio(sum(case["evidence_hit"] for case in answerable), len(answerable)),
        "mean_reciprocal_rank_at_k": (
            round(
                statistics.mean(
                    1 / case["first_relevant_rank"] if case["first_relevant_rank"] else 0
                    for case in answerable
                ),
                6,
            )
            if answerable
            else None
        ),
        "unanswerable_refusal_rate": ratio(sum(case["refused"] for case in unanswerable), len(unanswerable)),
        "answerable_false_refusal_rate": ratio(sum(case["refused"] for case in answerable), len(answerable)),
        "retrieval_latency_median_ms": round(statistics.median(case["retrieval_ms"] for case in results), 3),
        "llm_answer_accuracy": None,
        "human_answer_support_score": None,
    }


async def evaluate_cases(cases: list[dict]) -> list[dict]:
    from app.config import get_settings
    from app.db import SessionLocal
    from app.models import KnowledgeBase
    from app.providers import REFUSAL, stream_answer
    from app.rag import retrieve

    settings = get_settings()
    with SessionLocal() as session:
        kb_id = session.query(KnowledgeBase.id).scalar()
    results = []
    for case in cases:
        started = time.perf_counter()
        with SessionLocal() as session:
            chunks = retrieve(session, kb_id, case["question"], settings)
        retrieval_ms = (time.perf_counter() - started) * 1000
        relevant = [
            index
            for index, chunk in enumerate(chunks, 1)
            if chunk.filename == case["source"]
            and chunk.page_number == case["page"]
            and all(text in chunk.content for text in case["evidence"])
        ]
        answer = ""
        first_token_ms = None
        async for event in stream_answer(case["question"], chunks, settings):
            if event["type"] == "token":
                if first_token_ms is None:
                    first_token_ms = (time.perf_counter() - started) * 1000
                answer += event["content"]
        results.append(
            {
                **case,
                "evidence_hit": bool(relevant) if case["answerable"] else None,
                "first_relevant_rank": relevant[0] if relevant else None,
                "refused": answer == REFUSAL,
                "retrieval_ms": round(retrieval_ms, 3),
                "first_demo_token_ms": round(first_token_ms, 3) if first_token_ms is not None else None,
                "total_demo_ms": round((time.perf_counter() - started) * 1000, 3),
                "retrieved": [
                    {
                        "filename": chunk.filename,
                        "page": chunk.page_number,
                        "score": round(chunk.score, 6),
                        "content": chunk.content,
                    }
                    for chunk in chunks
                ],
                "demo_answer": answer,
                "prompt_tokens": None,
                "completion_tokens": None,
            }
        )
    return results


def run(args, all_cases: list[dict], root: Path) -> dict:
    configure_isolated_environment(root, args)
    from app.config import get_settings
    from app.db import SessionLocal, engine
    from app.ingestion import process_document
    from app.models import Base, Document, IngestionJob, KnowledgeBase, User, new_id
    from app.rag import embedding_fingerprint

    settings = get_settings()
    settings.storage_dir.mkdir(parents=True)
    Base.metadata.create_all(engine)
    corpus = {}
    started = time.perf_counter()
    try:
        with SessionLocal.begin() as session:
            owner_id, kb_id = new_id(), new_id()
            session.add(User(id=owner_id, email="offline-evaluation@example.invalid", password_hash="unused"))
            session.flush()
            session.add(KnowledgeBase(id=kb_id, owner_id=owner_id, name="Synthetic evaluation corpus"))
        for filename in sorted({case["source"] for case in all_cases if case["answerable"]}):
            source = args.dataset.parent / filename
            content = source.read_bytes()
            corpus[filename] = {"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}
            document_id, job_id = new_id(), new_id()
            destination = settings.storage_dir / (document_id + source.suffix)
            shutil.copyfile(source, destination)
            with SessionLocal.begin() as session:
                session.add(
                    Document(
                        id=document_id,
                        knowledge_base_id=kb_id,
                        filename=filename,
                        storage_path=str(destination),
                        sha256=corpus[filename]["sha256"],
                        size_bytes=len(content),
                    )
                )
                session.flush()
                session.add(IngestionJob(id=job_id, document_id=document_id))
            if process_document(job_id) != "succeeded":
                raise RuntimeError(f"Failed to ingest evaluation document {filename}")
            with SessionLocal() as session:
                corpus[filename]["chunks"] = session.get(Document, document_id).chunk_count
        ingestion_ms = (time.perf_counter() - started) * 1000
        selected = [case for case in all_cases if args.split == "all" or case["split"] == args.split]
        if not selected:
            raise ValueError("No cases match selected split")
        results = asyncio.run(evaluate_cases(selected))
        return {
            "schema_version": 1,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "mode": "OFFLINE_HASH_DEMO_BASELINE",
            "embedding_provider": "hash",
            "llm_provider": "demo",
            "embedding_fingerprint": embedding_fingerprint(settings),
            "database": "disposable SQLite; deleted after run",
            "external_model_calls": 0,
            "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
            "corpus": corpus,
            "configuration": {
                "top_k": args.top_k,
                "min_score": args.min_score,
                "chunk_size": settings.chunk_size,
                "chunk_overlap": settings.chunk_overlap,
                "embedding_dimensions": settings.embedding_dimensions,
                "concurrency": 1,
            },
            "runtime": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "sqlalchemy": importlib.metadata.version("sqlalchemy"),
            },
            "ingestion_ms": round(ingestion_ms, 3),
            "metrics": aggregate(results),
            "by_split": {
                split: aggregate([case for case in results if case["split"] == split])
                for split in ("dev", "test")
                if any(case["split"] == split for case in results)
            },
            "interpretation": [
                "Hash vectors are a lexical baseline, not a semantic embedding model.",
                "Demo output quotes up to three retrieved chunks; it does not generate an LLM answer.",
                "Evidence Hit@K requires the expected filename, page and all marked evidence in one retrieved chunk.",
                "Refusal is measured exactly; lexical overlap can retrieve irrelevant material for unanswerable questions.",
                "A high retrieval score is not an answer-support or answer-correctness score.",
                "The 24 hand-authored synthetic questions over three small files are not a representative production benchmark.",
                "Use dev to tune; report test separately. Published labels mean this is not a blind holdout.",
                "Latency is in-process, single-concurrency SQLite/demo timing, not HTTP or external LLM latency.",
                "No human answer-quality score, paid-model token usage, or production performance claim is inferred.",
            ],
            "results": results,
        }
    finally:
        engine.dispose()


def main() -> int:
    args = arguments()
    if not -1 <= args.min_score <= 1:
        raise SystemExit("--min-score must be between -1 and 1")
    if not 100 <= args.chunk_size <= 3000 or not 0 <= args.chunk_overlap < args.chunk_size:
        raise SystemExit("Require 100 <= --chunk-size <= 3000 and 0 <= --chunk-overlap < --chunk-size")
    args.dataset = args.dataset.resolve()
    all_cases = read_dataset(args.dataset)
    with tempfile.TemporaryDirectory(prefix="knowledge-evaluation-") as directory:
        report = run(args, all_cases, Path(directory))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"report": str(args.output.resolve()), "mode": report["mode"], "metrics": report["metrics"]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
