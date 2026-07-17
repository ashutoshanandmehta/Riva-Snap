"""Service configuration, loaded from environment / .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str = ""
    groq_api_key: str = ""
    fdc_api_key: str = "DEMO_KEY"

    # Supabase backend. When all three are set, scanning requires sign-in and
    # Accept persists logs; when unset the service runs in open stateless mode.
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    # Empty = auto-resolve the best vision model available on the account.
    riva_scan_model: str = ""
    riva_scan_debug: bool = True

    prompt_version: str = "v1"


@lru_cache
def settings() -> Settings:
    return Settings()
