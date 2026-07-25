"""Wellness suggestions: Claude picks 1-3 catalog practices for the user.

Text-only Messages call with a strict structured-output schema, mirroring
vision.py. The catalog ids must match the iOS `WellnessPractice.catalog`.
Callers (main.py) cache successful results per user-day in the
wellness_suggestions table and fall back to `fallback()` on ANY error —
fallback results are never cached, so transient failures retry next fetch.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import backend, vision
from .config import Settings

logger = logging.getLogger("scan.suggestions")

# id -> (kind, minutes, title). Must stay in sync with the iOS catalog.
CATALOG: dict[str, tuple[str, int, str]] = {
    "yoga_beginners": ("yoga", 12, "Yoga for complete beginners"),
    "yoga_weightloss": ("yoga", 18, "Weight loss yoga flow"),
    "yoga_digestion": ("yoga", 14, "Yoga for digestion"),
    "meditation_isha": ("meditation", 19, "Isha Kriya guided meditation"),
    "meditation_nsdr": ("meditation", 11, "NSDR non-sleep deep rest"),
    "exercise_walk": ("exercise", 17, "Low-impact walking workout"),
    "mind_gratitude": ("mind", 10, "Guided gratitude practice"),
    "sleep_winddown": ("sleep", 12, "Evening wind-down rest"),
}

DEFAULT_MODEL = "claude-sonnet-5"

# Strict Structured Output schema for the suggestion call.
SUGGEST_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["suggestions"],
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["practice_id", "reason"],
                "properties": {
                    "practice_id": {"type": "string", "enum": list(CATALOG)},
                    "reason": {
                        "type": "string",
                        "description": "Encouraging, specific, no medical claims.",
                    },
                },
            },
        },
    },
}

_SYSTEM_PROMPT = (
    "You are the wellness coach inside Riva, a companion app for people on"
    " GLP-1 medications. Pick 1 to 3 practices from the catalog that best fit"
    " the user's context right now. Rules: reasons are encouraging and"
    " specific, at most 120 characters, and make no medical claims or"
    " promises; prefer variety over the kinds the user already practiced this"
    " week; suggest the evening wind-down practice only in the evening; when"
    " recent sleep quality is low, favor NSDR."
)


def resolve_model(config: Settings) -> str:
    """Explicit RIVA_SUGGEST_MODEL override, else the Sonnet default."""
    return config.riva_suggest_model or DEFAULT_MODEL


def build_context(config: Settings, user_id: str) -> dict:
    """Everything the model (and the fallback rules) personalize on."""
    me = backend.get_me(config, user_id)
    tz_name = me["profile"].get("timezone") or "America/New_York"
    try:
        now_local = datetime.now(ZoneInfo(tz_name))
    except (KeyError, ValueError):
        now_local = datetime.now(timezone.utc)

    summary = backend.wellness_summary(config, user_id) or {}
    sessions = backend.list_wellness_sessions(config, user_id, 7)
    recent_kinds: dict[str, int] = {}
    for session in sessions:
        recent_kinds[session["kind"]] = recent_kinds.get(session["kind"], 0) + 1

    week_ago = (now_local.date() - timedelta(days=7)).isoformat()
    sleep = backend.list_sleep_checkins(config, user_id, week_ago)
    sleep_avg = round(sum(c["value"] for c in sleep) / len(sleep), 1) if sleep else None

    goal = me["nutrition_goals"].get("wellness_minutes_goal") or 45
    return {
        "day": summary.get("day", now_local.date().isoformat()),
        "local_hour": now_local.hour,
        "minutes_today": summary.get("minutes_today", 0),
        "goal_minutes": goal,
        "streak_days": summary.get("streak_days", 0),
        "recent_kinds": recent_kinds,
        "sleep_quality_avg": sleep_avg,  # 1 (terrible) .. 5 (excellent), or None
    }


def _catalog_text() -> str:
    return "\n".join(
        f"- {pid}: {title} ({kind}, {minutes} min)"
        for pid, (kind, minutes, title) in CATALOG.items()
    )


def _first_text(response: object) -> str | None:
    """First text block of a Messages response (skips any thinking blocks)."""
    return next((block.text for block in response.content if block.type == "text"), None)


def suggest(config: Settings, context: dict) -> dict:
    """One structured-output call -> {"suggestions": [...]}. Raises on any
    failure; the route catches and serves `fallback()` instead."""
    client = vision.make_client(config)
    model = resolve_model(config)
    user_text = (
        "Catalog:\n"
        + _catalog_text()
        + "\n\nUser context (local time of day, minutes practiced today vs goal,"
        " streak, this week's practice mix, average sleep quality 1-5):\n"
        + json.dumps(context, separators=(",", ":"))
    )
    extra: dict = {} if "haiku" in model else {"thinking": {"type": "disabled"}}
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema", "schema": SUGGEST_SCHEMA}},
        messages=[{"role": "user", "content": user_text}],
        **extra,
    )
    text = _first_text(response)
    if not text:
        raise ValueError("Empty response from suggestion model")
    return _validated(json.loads(text))


def _validated(payload: dict) -> dict:
    """Belt-and-braces on top of the schema: known ids, deduped, capped."""
    raw = payload.get("suggestions") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        raise ValueError("Suggestion payload missing 'suggestions' list")
    suggestions: list[dict] = []
    seen: set[str] = set()
    for entry in raw:
        practice_id = entry.get("practice_id") if isinstance(entry, dict) else None
        if practice_id not in CATALOG or practice_id in seen:
            continue
        seen.add(practice_id)
        suggestions.append(
            {"practice_id": practice_id, "reason": str(entry.get("reason", ""))[:120]}
        )
        if len(suggestions) == 3:
            break
    if not suggestions:
        raise ValueError("Suggestion payload had no valid catalog practices")
    return {"suggestions": suggestions}


def fallback(context: dict | None) -> dict:
    """Deterministic rules when the LLM path fails: low sleep -> NSDR,
    morning -> yoga, evening -> wind-down, else gratitude."""
    ctx = context or {}
    hour = ctx.get("local_hour", 12)
    sleep_avg = ctx.get("sleep_quality_avg")
    if sleep_avg is not None and sleep_avg <= 2.5:
        primary = (
            "meditation_nsdr",
            "Sleep has been rough lately — 10 minutes of deep rest can help you recharge.",
        )
    elif hour < 12:
        primary = ("yoga_beginners", "A gentle morning flow is a great way to start the day.")
    elif hour >= 19:
        primary = ("sleep_winddown", "Wind down this evening with a short rest practice.")
    else:
        primary = (
            "mind_gratitude",
            "A few mindful minutes of gratitude can lift the middle of your day.",
        )
    suggestions = [{"practice_id": primary[0], "reason": primary[1]}]
    if primary[0] != "mind_gratitude":
        suggestions.append(
            {
                "practice_id": "mind_gratitude",
                "reason": "A quick gratitude practice pairs well with any session.",
            }
        )
    return {"suggestions": suggestions}
