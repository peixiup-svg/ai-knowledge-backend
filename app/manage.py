import argparse


def main():
    parser = argparse.ArgumentParser(description="Knowledge backend maintenance commands")
    parser.add_argument("command", choices=["init-db", "recover-jobs"])
    args = parser.parse_args()
    if args.command == "init-db":
        from app.config import get_settings
        from app.db import init_db

        if get_settings().app_env == "production":
            parser.error("Production schema changes must use: alembic upgrade head")
        init_db()
        print("Database initialized.")
    else:
        from app.ingestion import recover_stale_jobs
        from app.tasks import dispatch_job, shutdown_local_workers

        ids = recover_stale_jobs()
        for job_id in ids:
            dispatch_job(job_id)
        shutdown_local_workers()
        print(f"Scheduled {len(ids)} recoverable jobs.")


if __name__ == "__main__":
    main()
