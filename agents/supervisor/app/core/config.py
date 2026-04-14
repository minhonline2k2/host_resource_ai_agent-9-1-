"""Supervisor Agent config."""
from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "supervisor_ai_agent"
    app_env: str = "development"
    app_debug: bool = False
    app_host: str = "0.0.0.0"
    app_port: int = 8082
    secret_key: str = "change-me"
    database_url: str = "mysql+asyncmy://ai_bot:123@db:3306/ai_alert_platform?charset=utf8mb4"
    redis_url: str = "redis://redis:6379/0"
    redis_dedup_ttl: int = 3600
    redis_approval_ttl: int = 3600
    redis_exec_lock_ttl: int = 600
    prometheus_url: str = "http://prometheus:9090"
    prometheus_timeout: int = 30
    ssh_user: str = "devops"
    ssh_key_path: str = "/app/ssh_keys/id_rsa"
    ssh_timeout: int = 30
    ssh_command_timeout: int = 60
    llm_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    llm_timeout: int = 360
    llm_max_retries: int = 3
    worker_concurrency: int = 4
    worker_poll_interval: int = 2
    auth_enabled: bool = False
    auth_token: str = "operator-token"
    orchestrator_url: str = ""
    agent_id: str = "supervisor-agent"
    agent_host: str = "supervisor-agent"
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache()
def get_settings() -> Settings:
    return Settings()
