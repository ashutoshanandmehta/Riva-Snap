"""Service configuration, loaded from environment / .env."""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    fdc_api_key: str = "DEMO_KEY"

    # Supabase backend. When all three are set, scanning requires sign-in and
    # Accept persists logs; when unset the service runs in open stateless mode.
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    # Empty = auto-resolve the best vision model available on the account.
    riva_scan_model: str = ""
    riva_scan_debug: bool = True
    # Wellness suggestions (app/suggestions.py). Empty = the Sonnet default.
    riva_suggest_model: str = ""
    # Food search / recipe decomposition (app/food_search.py). Naming and
    # composition are knowledge tasks. Empty = the Sonnet default.
    riva_food_search_model: str = ""

    # AI companion chat (app/chat/). Empty model = the Sonnet default.
    riva_chat_model: str = ""
    # Thinking depth / token spend for the chat loop. "medium" keeps an
    # interactive reply responsive; raise to "high" for harder reasoning.
    riva_chat_effort: str = "medium"
    # Versioned system prompt (app/chat/prompts/companion_<version>.md),
    # echoed in every conversational response. v2 adds the nutrition, wellness,
    # goals and to-do tools plus the write-confirmation rules; v1 predates the
    # write tools and must not be run with them enabled.
    riva_chat_prompt_version: str = "v2"
    # Tool-calling loop cap. Exceeded means answer with what the tools returned
    # so far rather than looping on the user's (and our) budget.
    riva_chat_max_tool_iterations: int = 4
    # Thread turns replayed into the prompt; older turns drop off.
    riva_chat_history_turns: int = 10

    prompt_version: str = "v1"

    # Volumetric segmentation (app/volumetric/segmenter.py). Empty token = the
    # classical (GrabCut) segmenter runs offline with no dependency.
    replicate_api_token: str = ""
    riva_sam2_model: str = "meta/sam-2"

    # Self-hosted SAM 2 (LitServe on a Lightning AI GPU Studio — see
    # backend/serving/sam2/). Takes priority over replicate_api_token when
    # both are set (no per-call billing, one round trip for the whole scan).
    # Empty endpoint = this backend is off.
    sam2_endpoint_url: str = ""
    sam2_api_key: str = ""
    sam2_timeout_s: float = 25.0

    # Dev-only capture persistence (app/volumetric/capture_store.py). Empty =
    # OFF (no writes); set to a dataset dir to bank every capture on disk for
    # offline re-scoring once the calibrated carver exists.
    volumetric_capture_dir: str = ""

    @field_validator(
        "anthropic_api_key",
        "fdc_api_key",
        "supabase_url",
        "supabase_anon_key",
        "supabase_service_role_key",
        "replicate_api_token",
        "sam2_endpoint_url",
        "sam2_api_key",
        "volumetric_capture_dir",
        mode="before",
    )
    @classmethod
    def strip_whitespace(cls, value: str) -> str:
        # Keys and URLs pasted into dashboards often pick up line wraps or
        # stray spaces, which become illegal HTTP header values.
        if isinstance(value, str):
            return "".join(value.split()).rstrip("/")
        return value


@lru_cache
def settings() -> Settings:
    return Settings()
