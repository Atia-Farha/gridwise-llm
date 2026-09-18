"""Application configuration — reads from environment variables / .env file."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    gemini_api_key: str
    gemini_model: str = "gemini-3.6-flash"
    port: int = 8000

    # Numeric tolerance for float comparisons (per spec §11.5)
    numeric_tolerance: float = 0.01

    # Small epsilon to detect near-zero LP values
    lp_epsilon: float = 1e-6

    # Tiny penalty added to objective to discourage simultaneous charge+discharge
    charge_discharge_penalty: float = 1e-4


settings = Settings()
