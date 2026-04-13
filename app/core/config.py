"""Application configuration from environment variables."""

from __future__ import annotations

import os
from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # App
    app_name: str = "host_resource_ai_agent"
    app_env: str = "development"
    app_debug: bool = False
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    secret_key: str = "change-me"

    # Database
    database_url: str = "mysql+asyncmy://agent:agent_secret@db:3306/host_resource_agent"

    # Redis
    redis_url: str = "redis://redis:6379/0"
    redis_dedup_ttl: int = 3600  # 1h — DB-level dedup sẽ bắt các alert lặp sau TTL
    redis_approval_ttl: int = 3600
    redis_exec_lock_ttl: int = 600

    # Prometheus
    prometheus_url: str = "http://prometheus:9090"
    prometheus_timeout: int = 30

    # SSH
    ssh_user: str = "devops"
    ssh_key_path: str = "/app/ssh_keys/id_rsa"
    ssh_timeout: int = 30
    ssh_command_timeout: int = 60

    # LLM
    llm_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    llm_timeout: int = 120
    llm_max_retries: int = 2

    # Worker
    worker_concurrency: int = 4
    worker_poll_interval: int = 2

    # Auth
    auth_enabled: bool = False
    auth_token: str = "operator-token"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache()
def get_settings() -> Settings:
    return Settings()
