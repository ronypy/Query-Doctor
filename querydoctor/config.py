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

    # Real validation builds a real index inside a rolled-back transaction
    # on the LOCAL demo database. Off unless explicitly enabled.
    enable_real_validation: bool = False
    real_validation_runs: int = 3

    # Savings model assumptions (stated in every report).
    calls_per_day: float | None = None      # None -> derive from pg_stat_statements
    vcpu_hour_price_usd: float = 0.04
    storage_gb_month_usd: float = 0.10


@lru_cache
def get_settings() -> Settings:
    return Settings()
