"""Vision call: one image in, strict-schema food analysis out.

Claude (Anthropic) via the native Messages API with structured outputs. The
model is used purely as a food *identifier* — portion/volume is handled
downstream by deterministic code — so a perception-tier model is enough.
Default is Claude Sonnet; override with RIVA_SCAN_MODEL (e.g. claude-opus-4-8
for quality, claude-haiku-4-5 for cost).
"""

import json
import logging
import re
from pathlib import Path

import anthropic

from .config import Settings

logger = logging.getLogger("scan.vision")

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Identification only, so a perception-tier model is the default.
DEFAULT_MODEL = "claude-sonnet-5"

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
            "description": "A brief, natural one-line sentence describing the food and its setting, mentioning the approximate portion size so it can help calibrate estimates. Write it the way a person would say it, with no dashes and no parentheses.",
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "name",
                    "food_class",
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
                    "food_class": {
                        "type": "string",
                        "enum": [
                            "burger",
                            "pizza_slice",
                            "rice",
                            "pasta",
                            "salad",
                            "soup",
                            "fries",
                            "meat",
                            "fruit",
                            "fried_snack",
                            "flatbread",
                            "curry_gravy",
                            "dal",
                            "sweet",
                            "other",
                        ],
                        "description": (
                            "Best-matching plausibility class for this item, for portion"
                            " gating downstream — 'other' if none clearly fit."
                        ),
                    },
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


def make_client(config: Settings) -> anthropic.Anthropic:
    if not config.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in backend/.env.")
    return anthropic.Anthropic(api_key=config.anthropic_api_key)


def resolve_model(config: Settings) -> str:
    """Explicit RIVA_SCAN_MODEL override, else the Sonnet default."""
    return config.riva_scan_model or DEFAULT_MODEL


def _nullable_to_anyof(node: object) -> object:
    """Rewrite JSON-Schema ``"type": [..., "null"]`` unions into ``anyOf``.

    Anthropic structured outputs accept anyOf but not the array-of-types form
    that SCAN_SCHEMA uses (e.g. ``"type": ["string", "null"]``). Keeping one
    schema source and transforming it avoids maintaining a second copy."""
    if isinstance(node, dict):
        node = {key: _nullable_to_anyof(value) for key, value in node.items()}
        type_field = node.get("type")
        if isinstance(type_field, list):
            siblings = {k: v for k, v in node.items() if k != "type"}
            variants = [{"type": tp, **siblings} for tp in type_field if tp != "null"]
            if "null" in type_field:
                variants.append({"type": "null"})
            return variants[0] if len(variants) == 1 else {"anyOf": variants}
        return node
    if isinstance(node, list):
        return [_nullable_to_anyof(item) for item in node]
    return node


_ANTHROPIC_SCHEMA = _nullable_to_anyof(SCAN_SCHEMA)


def _anthropic_text(response: object) -> str | None:
    """First text block of a Messages response (skips any thinking blocks)."""
    return next((block.text for block in response.content if block.type == "text"), None)


def analyze_image(
    client: anthropic.Anthropic,
    model: str,
    image_b64: str,
    hint: str | None,
    prompt_text: str,
    mode: str = "auto",
) -> dict:
    """Claude vision: one image + prompt -> schema-shaped dict.

    Thinking is disabled — this is a perception/identification task and the
    schema already constrains the output — except on Haiku, which predates the
    ``disabled`` option and simply runs without thinking by default."""
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

    image_block = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64},
    }
    extra: dict = {} if "haiku" in model else {"thinking": {"type": "disabled"}}

    try:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=prompt_text,
            output_config={"format": {"type": "json_schema", "schema": _ANTHROPIC_SCHEMA}},
            messages=[
                {"role": "user", "content": [image_block, {"type": "text", "text": user_text}]}
            ],
            **extra,
        )
        return _parse(_anthropic_text(response))
    except Exception as error:
        # Older SDKs / models may reject structured outputs — fall back to the
        # schema stated in the prompt.
        logger.warning(
            "Anthropic structured output failed (%s); retrying with schema in prompt",
            error,
        )

    fallback_text = user_text + (
        "\nReturn ONLY a JSON object that validates against this JSON Schema "
        "(no prose, no markdown):\n" + json.dumps(SCAN_SCHEMA, separators=(",", ":"))
    )
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=prompt_text,
        messages=[
            {"role": "user", "content": [image_block, {"type": "text", "text": fallback_text}]}
        ],
        **extra,
    )
    return _parse(_anthropic_text(response))


def _parse(content: str | None) -> dict:
    if not content:
        raise ValueError("Empty response from vision model")
    text = content.strip()
    # Defensive: reasoning models can leak think blocks, and some models add
    # markdown fences in fallback mode.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text)
