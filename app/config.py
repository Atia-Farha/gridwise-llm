"""Application configuration — reads from environment variables / .env file.

Only OPENAI_API_KEY is expected to be supplied by the operator. Every other
setting has a working default so the service always boots and GET /health
answers, even when the model provider is misconfigured.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Model provider ────────────────────────────────────────────────────
    # Deliberately defaulted to "" rather than required: a missing key must
    # degrade the interpretation step, not stop the service from starting.
    openai_api_key: str = ""
    openai_model: str = "gpt-5.6-luna"

    # Responses API reasoning budget: none | low | medium | high | xhigh | max
    # "low" keeps p95 latency inside the 5 s full-credit band while still
    # handling arithmetic notes such as "50% of capacity".
    openai_reasoning_effort: str = "low"

    # ── Latency budget ────────────────────────────────────────────────────
    # The judge hard-fails any request over 30 s. The LLM stage is capped
    # well below that so the optimizer always has room to run and reply.
    openai_timeout_seconds: float = 12.0
    openai_max_attempts: int = 3
    llm_deadline_seconds: float = 18.0

    # ── Cache (optional) ──────────────────────────────────────────────────
    # Empty REDIS_URL simply disables the shared cache; an in-process LRU
    # is always used as a fallback.
    redis_url: str = ""
    cache_ttl_seconds: int = 86400

    # ── Server ────────────────────────────────────────────────────────────
    port: int = 8000
    log_level: str = "INFO"

    # ── Numerics ──────────────────────────────────────────────────────────
    # Numeric tolerance for float comparisons (per Problem Statement §11.5)
    numeric_tolerance: float = 0.01

    # Small epsilon to detect near-zero LP values
    lp_epsilon: float = 1e-6

    # Tiny penalty added to objective to discourage simultaneous charge+discharge
    charge_discharge_penalty: float = 1e-4


settings = Settings()
