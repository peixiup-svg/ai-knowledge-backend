"""Create a private Docker profile from an existing real local model profile."""

import secrets
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def main():
    source, target = ROOT / ".env.local-models", ROOT / ".env.docker-local"
    if target.exists():
        raise SystemExit(
            "Docker profile already exists; edit it explicitly rather than replacing database credentials"
        )
    if not source.exists():
        raise SystemExit("Start run_local_stack.py once to create the private local profile first")
    values = {key: value for key, value in dotenv_values(source).items() if value is not None}
    values.update(
        POSTGRES_PASSWORD=secrets.token_hex(24),
        LLM_BASE_URL="http://host.docker.internal:11435/v1",
        EMBEDDING_BASE_URL="http://host.docker.internal:11436/v1",
        LLM_MODEL="qwen2.5-7b-instruct",
    )
    target.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8")
    print("Private Docker profile created. Do not commit or share it.")


if __name__ == "__main__":
    main()
