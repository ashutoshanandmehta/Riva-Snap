"""Vision call: one image in, strict-schema food analysis out.

Provider-agnostic via the OpenAI SDK: OpenAI directly, or Groq through its
OpenAI-compatible endpoint (fast, cheap Llama-4 vision). Chat Completions is
used because both providers support it identically.
"""

import json
import logging
import re
from pathlib import Path

from openai import OpenAI

from .config import Settings

logger = logging.getLogger("scan.vision")

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# Model preferences per provider. OpenAI entries are exact ids; Groq entries
# are substring tokens (their ids carry org prefixes and revision suffixes).
OPENAI_PREFERENCE = ["gpt-5.2", "gpt-5.1", "gpt-5", "gpt-4.1", "gpt-4o"]
OPENAI_FALLBACK = "gpt-4o"
GROQ_PREFERENCE_TOKENS = ["llama-4-maverick", "llama-4-scout"]

# Strict Structured Output schema for the vision call.
SCAN_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["scan_type", "reason", "plate", "items", "water"],
    "properties": {
        "scan_type": {
            "type": "string",
            "enum": ["food", "water", "beverage", "not_food"],
        },
        "reason": {
            "type": ["string", "null"],
            "description": "Only for not_food: short reason the image was rejected.",
        },
        "plate": {
            "type": ["string", "null"],
            "description": "Plate/bowl/container description incl. estimated size.",
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "name",
                    "portion_desc",
                    "portion_grams",
                    "is_liquid",
                    "confidence",
                    "calories",
                    "protein_g",
                    "carb_g",
                    "fiber_g",
                    "fat_g",
                    "sugar_g",
                    "sodium_mg",
                    "alternatives",
                ],
                "properties": {
                    "name": {"type": "string"},
                    "portion_desc": {"type": "string"},
                    "portion_grams": {
                        "type": "number",
                        "description": "Estimated grams (use ml for liquids).",
                    },
                    "is_liquid": {"type": "boolean"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "calories": {"type": "number"},
                    "protein_g": {"type": "number"},
                    "carb_g": {"type": "number"},
                    "fiber_g": {"type": "number"},
                    "fat_g": {"type": "number"},
                    "sugar_g": {"type": "number"},
                    "sodium_mg": {"type": "number"},
                    "alternatives": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Up to 2 alternate identifications.",
                    },
                },
            },
        },
        "water": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "required": ["container_type", "volume_oz", "glasses"],
            "properties": {
                "container_type": {"type": "string"},
                "volume_oz": {"type": "number"},
                "glasses": {
                    "type": "number",
                    "description": "8-oz glasses equivalent.",
                },
            },
        },
    },
}


def load_prompt(version: str) -> str:
    return (PROMPTS_DIR / f"scan_{version}.md").read_text()


def make_client(config: Settings) -> tuple[OpenAI, str]:
    """Returns (client, provider). OpenAI wins when both keys are set."""
    if config.openai_api_key:
        return OpenAI(api_key=config.openai_api_key), "openai"
    if config.groq_api_key:
        return OpenAI(api_key=config.groq_api_key, base_url=GROQ_BASE_URL), "groq"
    raise RuntimeError(
        "No LLM key configured. Set GROQ_API_KEY or OPENAI_API_KEY in scan-service/.env."
    )


def resolve_model(client: OpenAI, provider: str, override: str) -> str:
    """Picks the vision model: explicit override, else best available."""
    if override:
        return override
    try:
        available = [m.id for m in client.models.list()]
    except Exception as error:
        logger.warning("Could not list models (%s)", error)
        available = []

    if provider == "groq":
        for token in GROQ_PREFERENCE_TOKENS:
            for model_id in available:
                if token in model_id:
                    return model_id
        raise RuntimeError(
            "No vision-capable Llama-4 model available on this Groq account. "
            f"Available: {', '.join(available) or 'none'}"
        )

    for preferred in OPENAI_PREFERENCE:
        if preferred in available:
            return preferred
    return OPENAI_FALLBACK


def analyze_image(
    client: OpenAI,
    model: str,
    image_b64: str,
    hint: str | None,
    prompt_text: str,
    provider: str = "groq",
    mode: str = "auto",
) -> dict:
    """Runs the vision analysis and returns the parsed schema-shaped dict."""
    user_text = "Analyze this photo."
    # Mode steering is intentionally minimal: telling the model the user
    # "intends to log food" makes it FABRICATE meals on ambiguous images
    # (verified against a water-glass image). Food mode therefore adds no
    # perception bias at all — the mode-mismatch check happens server-side.
    # Water mode only asks for extra volume detail, plus a guard.
    if mode == "water":
        user_text += (
            " If the photo shows a drink, report its container and volume"
            " carefully (account for fill level and ice)."
            " Describe ONLY what is actually visible — if the photo shows food,"
            " classify it as food."
        )
    if hint:
        user_text += f" Context from the user: {hint}"

    # Low temperature keeps portion/nutrition estimates consistent scan-to-scan.
    # OpenAI's reasoning models reject the parameter, so Groq-only.
    extra_params: dict = {"temperature": 0.2} if provider == "groq" else {}

    messages = [
        {"role": "system", "content": prompt_text},
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                },
                {"type": "text", "text": user_text},
            ],
        },
    ]

    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "food_scan",
                    "strict": True,
                    "schema": SCAN_SCHEMA,
                },
            },
            **extra_params,
        )
        return _parse(response.choices[0].message.content)
    except Exception as error:
        # Some provider/model combos reject strict json_schema — fall back to
        # json_object mode with the schema stated in the prompt.
        logger.warning("json_schema mode failed (%s); retrying with json_object", error)

    messages[1]["content"][-1]["text"] += (
        "\nReturn ONLY a JSON object that validates against this JSON Schema "
        "(no prose, no markdown):\n" + json.dumps(SCAN_SCHEMA)
    )
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        response_format={"type": "json_object"},
        **extra_params,
    )
    return _parse(response.choices[0].message.content)


def _parse(content: str | None) -> dict:
    if not content:
        raise ValueError("Empty response from vision model")
    text = content.strip()
    # Defensive: strip markdown fences some models add in fallback mode.
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text)
