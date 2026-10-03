from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        extra="ignore",
    )

    llm_provider: str = "groq"
    llm_model: str = "openai/gpt-oss-120b"
    groq_api_key: SecretStr | None = None

    database_url: str = "postgresql://qd_agent:qd_agent@localhost:5433/tpch"
    admin_database_url: str = "postgresql://postgres:postgres@localhost:5433/tpch"

    # Critic requires at least this fractional planner-cost reduction.
    improvement_threshold: float = 0.30
    max_iterations: int = 3
    statement_timeout_ms: int = 30000
    demo_mode: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
