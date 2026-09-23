"""Run real local LLM + embedding services + application until Ctrl+C.

The application interpreter is the project .venv. Pass --inference-python for an
interpreter with requirements-local.txt installed. Existing demo data is retained.
"""

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def create_profile():
    target = ROOT / ".env.local-models"
    if target.exists():
        return
    token = secrets.token_urlsafe(36)
    content = {
        "APP_ENV": "local",
        "DATABASE_URL": "sqlite:///./data/knowledge-local-ai.db",
        "STORAGE_DIR": "data/uploads-local-ai",
        "JWT_SECRET": secrets.token_urlsafe(48),
        "TASK_MODE": "local",
        "RATE_LIMIT_BACKEND": "memory",
        "AUTO_CREATE_SCHEMA": "true",
        "LLM_PROVIDER": "openai",
        "LLM_BASE_URL": "http://127.0.0.1:11435/v1",
        "LLM_MODEL": "qwen2.5-1.5b-instruct",
        "LLM_API_KEY": token,
        "LLM_TIMEOUT_SECONDS": "120",
        "LLM_MAX_TOKENS": "384",
        "MAX_CONCURRENT_GENERATIONS": "1",
        "EMBEDDING_PROVIDER": "openai",
        "EMBEDDING_BASE_URL": "http://127.0.0.1:11436/v1",
        "EMBEDDING_MODEL": "bge-small-zh-v1.5",
        "EMBEDDING_DIMENSIONS": "512",
        "EMBEDDING_API_KEY": token,
        "CHUNK_SIZE": "350",
        "CHUNK_OVERLAP": "50",
        "RETRIEVAL_TOP_K": "3",
        "RETRIEVAL_MIN_SCORE": "0.35",
        "CHAT_REQUESTS_PER_MINUTE": "120",
    }
    target.write_text(
        "# Real local inference profile; keep this file private.\n"
        + "\n".join(f"{key}={value}" for key, value in content.items())
        + "\n",
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inference-python", default="python", help="Python with torch and sentence-transformers"
    )
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--gpu-layers", type=int, default=99, help="Use 0 for CPU-only inference")
    parser.add_argument("--model-file", type=Path, help="Local GGUF; for split models select the first shard")
    parser.add_argument("--model-name", help="Model alias reported by the API")
    args = parser.parse_args()
    for port in (args.api_port, 11435, 11436):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise SystemExit(
                    f"Port {port} is occupied; stop the existing service before starting this stack"
                )
    create_profile()
    environment = os.environ.copy()
    environment.update(
        {key: value for key, value in dotenv_values(ROOT / ".env.local-models").items() if value is not None}
    )
    environment.update(
        PYTHONIOENCODING="utf-8", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false"
    )
    binaries = list((ROOT / "data/runtime").rglob("llama-server.exe"))
    if len(binaries) != 1:
        raise SystemExit("Expected one verified llama-server.exe under data/runtime; see docs/local-ai.md")
    model_path = ROOT / "data/local-models/qwen2.5-1.5b/qwen2.5-1.5b-instruct-q4_k_m.gguf"
    if args.model_file:
        model_path = args.model_file.resolve()
    if args.model_name:
        environment["LLM_MODEL"] = args.model_name
    if not model_path.is_file():
        raise SystemExit("Download the verified Qwen GGUF model before starting")
    # llama.cpp accepts LLAMA_API_KEY without exposing the token in process arguments.
    environment["LLAMA_API_KEY"] = environment["LLM_API_KEY"]
    commands = [
        (
            "llm",
            [
                str(binaries[0]),
                "-m",
                str(model_path),
                "--host",
                "127.0.0.1",
                "--port",
                "11435",
                "-c",
                "4096",
                "-ngl",
                str(args.gpu_layers),
                "--parallel",
                "1",
                "--alias",
                environment["LLM_MODEL"],
                "--no-webui",
            ],
            "http://127.0.0.1:11435/health",
        ),
        (
            "embedding",
            [
                args.inference_python,
                "-m",
                "uvicorn",
                "local_inference.embedding_server:app",
                "--host",
                "127.0.0.1",
                "--port",
                "11436",
            ],
            "http://127.0.0.1:11436/health",
        ),
        (
            "api",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(args.api_port),
            ],
            f"http://127.0.0.1:{args.api_port}/ready",
        ),
    ]
    logs = ROOT / "data/local-stack-logs"
    logs.mkdir(parents=True, exist_ok=True)
    processes, streams = [], []
    try:
        for name, command, health_url in commands:
            stream = (logs / f"{name}.log").open("w", encoding="utf-8")
            streams.append(stream)
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=environment,
                stdout=stream,
                stderr=stream,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            processes.append(process)
            deadline = time.monotonic() + 180
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"{name} exited; see data/local-stack-logs/{name}.log")
                try:
                    response = httpx.get(health_url, timeout=2, trust_env=False)
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{name} readiness timed out")
                time.sleep(0.3)
            print(json.dumps({"service": name, "status": "ready", "pid": process.pid}), flush=True)
        print(
            f"Real local AI stack ready: http://127.0.0.1:{args.api_port}/docs (Ctrl+C stops owned processes)",
            flush=True,
        )
        while all(process.poll() is None for process in processes):
            time.sleep(1)
        raise RuntimeError("A child service exited unexpectedly; check local-stack-logs")
    except KeyboardInterrupt:
        print("Stopping local stack.", flush=True)
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
        for process in reversed(processes):
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        for stream in streams:
            stream.close()


if __name__ == "__main__":
    main()
