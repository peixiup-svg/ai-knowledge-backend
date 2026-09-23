"""Explicit local Docker acceptance: pauses worker briefly, restores it in finally."""

import argparse
import json
import subprocess
import time
import uuid
from pathlib import Path

import httpx
from verify_live_stack import require

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--env-file", default=".env.docker-local")
    args = parser.parse_args()
    prefix = [args.docker, "compose", "--env-file", args.env_file]

    def compose(*command):
        result = subprocess.run(
            prefix + list(command),
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90,
            check=True,
        )
        return result.stdout

    report = {"status": "running", "scope": "Local Docker/PostgreSQL/Redis/Celery; one synthetic upload"}
    kb_id = None
    worker_stopped = False
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=30, trust_env=False) as client:
        assert require(client.get("/health"))["task_mode"] == "celery"
        identity = {
            "email": f"recovery-{uuid.uuid4().hex[:12]}@example.com",
            "password": "Recovery-" + uuid.uuid4().hex,
        }
        require(client.post("/api/v1/auth/register", json=identity), 201)
        token = require(client.post("/api/v1/auth/login", json=identity))["access_token"]
        client.headers["Authorization"] = "Bearer " + token
        try:
            kb_id = require(client.post("/api/v1/knowledge-bases", json={"name": "Queue recovery"}), 201)[
                "id"
            ]
            worker_stopped = True
            compose("stop", "worker")
            upload = require(
                client.post(
                    f"/api/v1/knowledge-bases/{kb_id}/documents",
                    files={
                        "file": (
                            "recovery.txt",
                            "Queue recovery check. This document tests durable delivery.",
                            "text/plain",
                        )
                    },
                ),
                202,
            )
            job_id, document_id = upload["job_id"], upload["document_id"]
            time.sleep(2)
            queued = require(client.get(f"/api/v1/jobs/{job_id}"))
            assert queued["status"] == "queued", queued
            report["queued_while_worker_stopped"] = True
            compose("start", "worker")
            worker_stopped = False
            deadline = time.monotonic() + 120
            while True:
                job = require(client.get(f"/api/v1/jobs/{job_id}"))
                if job["status"] == "succeeded":
                    break
                assert job["status"] not in {"failed", "cancelled"}, job
                if time.monotonic() > deadline:
                    raise TimeoutError("Worker did not drain the queued upload")
                time.sleep(0.5)
            report["processed_after_worker_restart"] = True
            # Re-deliver the same successful job and wait for its actual worker result.
            code = """import json
from sqlalchemy import select,func
from app.db import SessionLocal
from app.models import DocumentChunk,IngestionJob
from app.tasks import ingest_task
job_id = JOB
document_id = DOC
def snapshot():
    with SessionLocal() as db:
        return [db.scalar(select(func.count()).select_from(DocumentChunk).where(DocumentChunk.document_id == document_id)), db.get(IngestionJob,job_id).attempts]
before=snapshot()
task=ingest_task.apply_async(args=[job_id],ignore_result=False)
task.get(timeout=30)
after=snapshot()
assert before == after and before[0] > 0, (before,after)
task.forget()
print(json.dumps({'before':before,'after':after,'duplicate_delivery_passed':True}))
""".replace("JOB", repr(job_id)).replace("DOC", repr(document_id))
            report.update(json.loads(compose("exec", "-T", "api", "python", "-c", code)))
            report["status"] = "passed"
        finally:
            if worker_stopped:
                compose("start", "worker")
            if kb_id:
                require(client.delete(f"/api/v1/knowledge-bases/{kb_id}"), 204)
            (ROOT / "reports/docker-recovery.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
