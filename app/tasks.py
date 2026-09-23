"""Local mode uses one process; production uses persistent Celery delivery."""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from celery import Celery

from app.config import get_settings
from app.ingestion import process_document, recover_stale_jobs

logger = logging.getLogger(__name__)
celery_app = Celery("knowledge", broker=get_settings().redis_url, backend=get_settings().redis_url)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_transport_options={"visibility_timeout": get_settings().job_lease_seconds + 300},
    broker_connection_retry_on_startup=True,
    task_ignore_result=True,
    beat_schedule={"recover-leases": {"task": "knowledge.recover", "schedule": 60.0}},
)


@celery_app.task(bind=True, name="knowledge.ingest", max_retries=10)
def ingest_task(self, job_id: str):
    status = process_document(job_id)
    if status == "queued":
        raise self.retry(countdown=min(2 ** (self.request.retries + 1), 30))


@celery_app.task(name="knowledge.recover")
def recover_task():
    for job_id in recover_stale_jobs():
        ingest_task.delay(job_id)


_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def _run_local(job_id: str) -> None:
    for attempt in range(get_settings().job_max_attempts):
        if process_document(job_id) != "queued":
            break
        time.sleep(min(2**attempt, 8))


def dispatch_job(job_id: str) -> None:
    global _pool
    if get_settings().task_mode == "celery":
        # DB row is the durable source of truth if broker publish fails;
        # periodic recovery will publish it after Redis is reachable.
        ingest_task.apply_async(args=[job_id], retry=False)
    else:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ingestion")
            _pool.submit(_run_local, job_id)


def shutdown_local_workers() -> None:
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=True)
