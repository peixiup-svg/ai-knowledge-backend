"""Run recorded real-model checks against an already running application.

This creates one temporary knowledge base and soft-deletes it afterward. It never
claims production quality from the small, published synthetic regression set.
"""

import argparse
import json
import re
import statistics
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def require(response, status=200):
    if response.status_code != status:
        raise RuntimeError(f"{response.request.url.path}: HTTP {response.status_code}: {response.text[:200]}")
    return response.json() if response.content else {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/local-ai-live.json")
    parser.add_argument("--split", choices=("dev", "test", "all"), default="all")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--require-task-mode", choices=("local", "celery"))
    args = parser.parse_args()
    cases = [
        json.loads(line)
        for line in (ROOT / "examples/evaluation.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    cases = [case for case in cases if args.split == "all" or case["split"] == args.split]
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "mode": "REAL_LOCAL_MODELS",
        "split": args.split,
        "top_k": args.top_k,
        "results": [],
        "limits": [
            "24 public synthetic cases; not an independent blind benchmark",
            "Refusal and citation checks are syntactic heuristics, not human-scored answer accuracy",
            "Timing is end-to-end HTTP at concurrency 1 on this machine; no production load claim",
            "Local models have no API billing; electricity and hardware costs are not estimated",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    kb_id = None
    password = "Live-" + uuid.uuid4().hex
    email = f"live-{uuid.uuid4().hex[:12]}@example.com"
    with httpx.Client(
        base_url=args.base_url, timeout=httpx.Timeout(150, connect=5), trust_env=False
    ) as client:
        report["health"] = require(client.get("/health"))
        if args.require_task_mode and report["health"].get("task_mode") != args.require_task_mode:
            raise SystemExit("Actual task mode does not match the requested verification mode")
        if report["health"]["llm_provider"] != "openai" or report["health"]["embedding_provider"] != "openai":
            raise SystemExit("Refusing to label hash/demo results as real-model verification")
        require(client.get("/ready"))
        require(client.post("/api/v1/auth/register", json={"email": email, "password": password}), 201)
        token = require(client.post("/api/v1/auth/login", json={"email": email, "password": password}))[
            "access_token"
        ]
        client.headers["Authorization"] = f"Bearer {token}"
        try:
            kb_id = require(client.post("/api/v1/knowledge-bases", json={"name": "真实模型回归验收"}), 201)[
                "id"
            ]
            outsider_email = f"outsider-{uuid.uuid4().hex[:12]}@example.com"
            with httpx.Client(base_url=args.base_url, timeout=30, trust_env=False) as outsider:
                require(
                    outsider.post(
                        "/api/v1/auth/register", json={"email": outsider_email, "password": password}
                    ),
                    201,
                )
                outsider_token = require(
                    outsider.post("/api/v1/auth/login", json={"email": outsider_email, "password": password})
                )["access_token"]
                outsider.headers["Authorization"] = f"Bearer {outsider_token}"
                require(
                    outsider.post(
                        f"/api/v1/knowledge-bases/{kb_id}/search", json={"question": "permission check"}
                    ),
                    404,
                )
                require(outsider.delete(f"/api/v1/knowledge-bases/{kb_id}"), 404)
                report["cross_user_isolation_passed"] = True
            uploads = []
            for name in ("software-guide.md", "expense-policy.txt", "course-python.md"):
                data = (ROOT / "examples" / name).read_bytes()
                upload = require(
                    client.post(
                        f"/api/v1/knowledge-bases/{kb_id}/documents",
                        files={"file": (name, data, "text/plain")},
                    ),
                    202,
                )
                deadline = time.monotonic() + 150
                while True:
                    job = require(client.get(f"/api/v1/jobs/{upload['job_id']}"))
                    if job["status"] == "succeeded":
                        break
                    if job["status"] in {"failed", "cancelled"} or time.monotonic() > deadline:
                        raise RuntimeError(f"Ingestion did not succeed: {job}")
                    time.sleep(0.2)
                uploads.append(upload)
            # Same content must not produce another task or document.
            duplicate = require(
                client.post(
                    f"/api/v1/knowledge-bases/{kb_id}/documents",
                    files={
                        "file": (
                            "software-guide.md",
                            (ROOT / "examples/software-guide.md").read_bytes(),
                            "text/markdown",
                        )
                    },
                ),
                202,
            )
            report["duplicate_upload_passed"] = (
                duplicate["deduplicated"] and duplicate["document_id"] == uploads[0]["document_id"]
            )
            for case in cases:
                search = require(
                    client.post(
                        f"/api/v1/knowledge-bases/{kb_id}/search",
                        json={"question": case["question"], "top_k": args.top_k},
                    )
                )
                citations = search["citations"]
                ranks = [
                    index
                    for index, source in enumerate(citations, 1)
                    if case["answerable"]
                    and source["filename"] == case["source"]
                    and source["page_number"] == case["page"]
                    and all(evidence in source["content"] for evidence in case["evidence"])
                ]
                start, first = time.perf_counter(), None
                answer, events, event_name = "", [], None
                with client.stream(
                    "POST",
                    f"/api/v1/knowledge-bases/{kb_id}/chat",
                    json={"question": case["question"], "top_k": args.top_k},
                ) as response:
                    if response.status_code != 200:
                        raise RuntimeError(f"Chat HTTP {response.status_code}")
                    for line in response.iter_lines():
                        if line.startswith("event:"):
                            event_name = line[6:].strip()
                        elif line.startswith("data:"):
                            payload = json.loads(line[5:].strip())
                            events.append((event_name, payload))
                            if event_name == "token":
                                if first is None:
                                    first = (time.perf_counter() - start) * 1000
                                answer += payload["content"]
                dones = [value for name, value in events if name == "done"]
                errors = [value for name, value in events if name == "error"]
                referenced = [int(number) for number in re.findall(r"\[(\d+)\]", answer)]
                refused = any(
                    fragment in answer
                    for fragment in (
                        "无法确认",
                        "无法确定",
                        "未提供",
                        "没有提供",
                        "未提及",
                        "未明确",
                        "没有明确",
                        "没有找到",
                        "没有提及",
                        "信息不足",
                        "资料不足",
                    )
                )
                result = {
                    "id": case["id"],
                    "split": case["split"],
                    "question": case["question"],
                    "expected_answerable": case["answerable"],
                    "evidence_hit": bool(ranks),
                    "evidence_rank": min(ranks) if ranks else None,
                    "answer": answer,
                    "citations": citations,
                    "reference_numbers": referenced,
                    "references_in_range": all(1 <= index <= len(citations) for index in referenced),
                    "refusal_heuristic": refused,
                    "first_token_ms": round(first, 2) if first else None,
                    "total_ms": round((time.perf_counter() - start) * 1000, 2),
                    "errors": errors,
                    "completion": dones[-1] if dones else None,
                }
                report["results"].append(result)
                save()
                print(
                    json.dumps(
                        {"id": case["id"], "hit": result["evidence_hit"], "answer": answer, "errors": errors},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            report["usage"] = require(client.get("/api/v1/usage"))
            require(client.delete(f"/api/v1/documents/{uploads[0]['document_id']}"), 204)
            remaining = require(
                client.post(
                    f"/api/v1/knowledge-bases/{kb_id}/search", json={"question": "DATABASE_URL 数据库连接"}
                )
            )["citations"]
            report["deletion_exclusion_passed"] = all(
                source["document_id"] != uploads[0]["document_id"] for source in remaining
            )
            rows = report["results"]
            answerable = [row for row in rows if row["expected_answerable"]]
            negative = [row for row in rows if not row["expected_answerable"]]
            report["metrics"] = {
                "questions": len(rows),
                "answerable": len(answerable),
                "unanswerable": len(negative),
                "evidence_hit_at_k": sum(row["evidence_hit"] for row in answerable) / len(answerable),
                "heuristic_unanswerable_refusal_rate": sum(row["refusal_heuristic"] for row in negative)
                / len(negative),
                "answerable_with_in_range_reference_rate": sum(
                    bool(row["reference_numbers"]) and row["references_in_range"] for row in answerable
                )
                / len(answerable),
                "median_first_token_ms": statistics.median(
                    row["first_token_ms"] for row in rows if row["first_token_ms"] is not None
                ),
                "median_total_ms": statistics.median(row["total_ms"] for row in rows),
                "completed": sum(
                    bool(row["completion"]) and row["completion"]["status"] == "completed" for row in rows
                ),
                "human_answer_accuracy": None,
            }
            report["status"] = (
                "passed"
                if report["metrics"]["completed"] == len(rows)
                and report["duplicate_upload_passed"]
                and report["deletion_exclusion_passed"]
                else "failed"
            )
        finally:
            if kb_id:
                require(client.delete(f"/api/v1/knowledge-bases/{kb_id}"), 204)
            save()
    print(
        json.dumps({"status": report["status"], "metrics": report.get("metrics")}, ensure_ascii=False),
        flush=True,
    )
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
