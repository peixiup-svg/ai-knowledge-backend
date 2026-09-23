"""Verify an actual running API; creates one temporary KB and soft-deletes it afterward."""

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import httpx


def expect(response: httpx.Response, status: int) -> dict | list:
    if response.status_code != status:
        raise RuntimeError(
            f"{response.request.method} {response.request.url.path}: "
            f"expected {status}, received {response.status_code}: {response.text[:300]}"
        )
    return response.json() if response.content else {}


def run(base_url: str, timeout: float) -> None:
    project = Path(__file__).resolve().parents[1]
    email = f"smoke-{uuid.uuid4().hex[:12]}@example.com"
    password = f"Smoke-{uuid.uuid4().hex}!"
    kb_id = None
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout) as client:
        expect(client.get("/ready"), 200)
        expect(client.post("/api/v1/auth/register", json={"email": email, "password": password}), 201)
        login = expect(client.post("/api/v1/auth/login", json={"email": email, "password": password}), 200)
        client.headers["Authorization"] = f"Bearer {login['access_token']}"
        try:
            kb = expect(client.post("/api/v1/knowledge-bases", json={"name": "Smoke 流程验证"}), 201)
            kb_id = kb["id"]
            filename = "software-guide.md"
            payload = (project / "examples" / filename).read_bytes()
            upload = expect(
                client.post(
                    f"/api/v1/knowledge-bases/{kb_id}/documents",
                    files={"file": (filename, payload, "text/markdown")},
                ),
                202,
            )
            deadline = time.monotonic() + timeout
            while True:
                job = expect(client.get(f"/api/v1/jobs/{upload['job_id']}"), 200)
                if job["status"] in {"completed", "succeeded", "ready"}:
                    break
                if job["status"] in {"failed", "cancelled"}:
                    raise RuntimeError(f"Ingestion failed: {job.get('error')}")
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Ingestion did not finish within {timeout} seconds")
                time.sleep(0.2)
            question = "星河文档服务的数据库连接环境变量是什么？"
            result = expect(
                client.post(f"/api/v1/knowledge-bases/{kb_id}/search", json={"question": question}), 200
            )
            if not result["citations"]:
                raise RuntimeError("Search returned no source citations")
            events = []
            event_name = "message"
            with client.stream(
                "POST", f"/api/v1/knowledge-bases/{kb_id}/chat", json={"question": question}
            ) as response:
                if response.status_code != 200:
                    raise RuntimeError(f"Chat returned HTTP {response.status_code}")
                for line in response.iter_lines():
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        events.append((event_name, json.loads(line[5:].strip())))
            errors = [data for name, data in events if name == "error"]
            if errors:
                raise RuntimeError(f"Chat failed: {errors}")
            sources = [data for name, data in events if name == "sources"]
            done = [data for name, data in events if name == "done"]
            if not sources or not sources[0].get("citations") or not done:
                raise RuntimeError("Chat did not return source citations and completion event")
            print(
                json.dumps(
                    {
                        "status": "passed",
                        "account": email,
                        "retrieved_sources": len(result["citations"]),
                        "sse_events": [name for name, _ in events],
                        "answer": "".join(
                            data.get("content", "") for name, data in events if name == "token"
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            if kb_id:
                expect(client.delete(f"/api/v1/knowledge-bases/{kb_id}"), 204)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        run(args.base_url, args.timeout)
    except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
        print(f"Smoke failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
