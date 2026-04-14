from functools import lru_cache
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    app_name: str = "orchestrator"
    app_port: int = 8080
    app_debug: bool = False
    secret_key: str = "change-me"
    database_url: str = "mysql+asyncmy://ai_bot:123@db:3306/ai_alert_platform?charset=utf8mb4"
    redis_url: str = "redis://redis:6379/0"
    redis_dedup_ttl: int = 300
    ui_base_url: str = "http://localhost:3000"
    teams_webhook_url: str = ""
    teams_enabled: bool = True
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

@lru_cache()
def get_settings() -> Settings:
    return Settings()
