from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["local", "production", "test"] = "local"
    database_url: str = "sqlite:///./data/knowledge.db"
    storage_dir: Path = Path("data/uploads")
    jwt_secret: SecretStr = SecretStr("local-demo-only-change-before-deployment-32chars")
    jwt_expire_minutes: int = Field(default=120, ge=1, le=10080)
    task_mode: Literal["local", "celery"] = "local"
    redis_url: str = "redis://localhost:6379/0"
    rate_limit_backend: Literal["memory", "redis"] = "memory"
    chat_requests_per_minute: int = Field(default=20, ge=1)
    auth_requests_per_minute: int = Field(default=20, ge=1)
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    max_document_chars: int = Field(default=1_000_000, ge=1000)
    max_pdf_pages: int = Field(default=300, ge=1)
    chunk_size: int = Field(default=600, ge=100, le=3000)
    chunk_overlap: int = Field(default=100, ge=0)
    embedding_provider: Literal["hash", "openai"] = "hash"
    embedding_dimensions: int = Field(default=256, ge=8, le=2000)
    embedding_model: str = "text-embedding-3-small"
    embedding_api_key: SecretStr = SecretStr("")
    embedding_base_url: str = "https://api.openai.com/v1"
    llm_provider: Literal["demo", "openai"] = "demo"
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "configure-your-model"
    llm_timeout_seconds: float = Field(default=60, gt=0, le=300)
    llm_max_tokens: int = Field(default=800, ge=64, le=8192)
    llm_temperature: float = Field(default=0.0, ge=0, le=2)
    max_concurrent_generations: int = Field(default=4, ge=1, le=100)
    retrieval_top_k: int = Field(default=5, ge=1, le=20)
    retrieval_min_score: float = Field(default=0.08, ge=-1, le=1)
    job_lease_seconds: int = Field(default=900, ge=30)
    job_max_attempts: int = Field(default=3, ge=1, le=10)
    auto_create_schema: bool = True

    @model_validator(mode="after")
    def validate_configuration(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("CHUNK_OVERLAP must be smaller than CHUNK_SIZE")
        if self.llm_provider == "openai" and not self.llm_api_key.get_secret_value():
            raise ValueError("LLM_API_KEY is required for the openai provider")
        if self.embedding_provider == "openai" and not self.embedding_api_key.get_secret_value():
            raise ValueError("EMBEDDING_API_KEY is required for the openai provider")
        if self.app_env == "production":
            secret = self.jwt_secret.get_secret_value()
            if len(secret) < 32 or secret.startswith("local-demo"):
                raise ValueError("Production requires a random JWT_SECRET with at least 32 characters")
            if not self.database_url.startswith("postgresql"):
                raise ValueError("Production requires PostgreSQL")
            if self.task_mode != "celery" or self.rate_limit_backend != "redis":
                raise ValueError("Production requires Celery and Redis rate limiting")
            if self.auto_create_schema:
                raise ValueError("Production uses Alembic; set AUTO_CREATE_SCHEMA=false")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
