"""Service configuration, loaded from environment / .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str = ""
    groq_api_key: str = ""
    fdc_api_key: str = "DEMO_KEY"
    # Empty = auto-resolve the best vision model available on the account.
    riva_scan_model: str = ""
    riva_scan_debug: bool = True

    prompt_version: str = "v1"


@lru_cache
def settings() -> Settings:
    return Settings()
