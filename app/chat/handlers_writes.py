"""Write handlers: the only place chat mutates the user's data.

Each one is reached **only** after `confirm.py` has established that the user
saw exactly these values in an earlier turn and agreed. That gate is upstream,
in `tools.dispatch`, so nothing here has to police it — but nothing here may be
called from anywhere else either.

Validation is deliberately stricter than the schema. A JSON Schema `minimum`
is a hint to the model; these bounds are the actual contract, and a value
outside them is a clear 400 the model can correct, never a silently stored
outlier in a medical log.
"""

import logging

from fastapi import HTTPException

from .. import backend
from ..config import Settings
from .handlers import iso_date

logger = logging.getLogger("scan.chat.handlers_writes")

# Bounds the database itself does not enforce on `weights.pounds`. Wide enough
# for any real person, narrow enough to catch a units mix-up (kg typed as lbs).
MIN_POUNDS = 50.0
MAX_POUNDS = 1000.0

# The nine effects `side_effect_log_items.effect` accepts, in the order the
# app's sheet lists them. Mirrored here so a bad value is a 400 and not a 500
# from a check-constraint violation.
SIDE_EFFECTS = (
    "nausea",
    "headache",
    "fatigue",
    "constipation",
    "diarrhea",
    "dizziness",
    "bloating",
    "heartburn",
    "food_noise",
)
MAX_SEVERITY = 5
MAX_NOTE_CHARS = 500

TODO_CATEGORIES = ("food", "water", "weight", "custom")
TODO_REPEAT_RULES = ("daily", "once")
MAX_TITLE_CHARS = 80


def _number(value: object, field: str) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field} must be a number.") from None


def _whole(value: object, field: str, low: int, high: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field} must be a whole number.") from None
    if not low <= parsed <= high:
        raise HTTPException(status_code=400, detail=f"{field} must be between {low} and {high}.")
    return parsed


def _text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail=f"{field} is required.")
    text = value.strip()
    if len(text) > limit:
        raise HTTPException(status_code=400, detail=f"{field} must be {limit} characters or less.")
    return text


def _choice(value: object, field: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise HTTPException(
            status_code=400, detail=f"{field} must be one of: {', '.join(allowed)}."
        )
    return str(value)


def _todo_id(arguments: dict, required: bool = True) -> str | None:
    value = arguments.get("todo_id")
    if value in (None, ""):
        if required:
            raise HTTPException(
                status_code=400,
                detail="todo_id is required. Call retrieve_todos to get it; do not invent one.",
            )
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="todo_id must be an id from retrieve_todos.")
    return value


# ---------------------------------------------------------------------------
# Health data
# ---------------------------------------------------------------------------


def record_weight(config: Settings, user_id: str, arguments: dict) -> dict:
    """Append one weigh-in. `log_weight` computes the day in the profile tz."""
    pounds = round(_number(arguments.get("pounds"), "pounds"), 2)
    if not MIN_POUNDS <= pounds <= MAX_POUNDS:
        raise HTTPException(
            status_code=400,
            detail=f"pounds must be between {MIN_POUNDS:.0f} and {MAX_POUNDS:.0f}.",
        )
    measured_at = iso_date(arguments.get("measured_at"), "measured_at")

    row = backend.log_weight(config, user_id, pounds, measured_at)
    logger.info("record_weight: saved")
    return {
        "status": "saved",
        "saved": {"pounds": row.get("pounds"), "measured_at": row.get("measured_at")},
    }


def _validated_effects(value: object) -> list[dict]:
    """The submitted set, as `log_side_effects` wants it: effect + severity."""
    if not isinstance(value, list) or not value:
        raise HTTPException(
            status_code=400,
            detail=(
                "effects must be a list of objects like"
                ' {"effect": "nausea", "severity": 3}, severity 1 (mild) to 5 (severe).'
            ),
        )
    cleaned: list[dict] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=400,
                detail='each effect must be an object with "effect" and "severity".',
            )
        effect = _choice(item.get("effect"), "effect", SIDE_EFFECTS)
        if effect in seen:
            raise HTTPException(status_code=400, detail=f"{effect} is listed twice.")
        seen.add(effect)
        cleaned.append(
            {
                "effect": effect,
                "severity": _whole(item.get("severity"), "severity", 1, MAX_SEVERITY),
            }
        )
    return cleaned


def record_side_effects(config: Settings, user_id: str, arguments: dict) -> dict:
    """Replace today's side-effect set.

    `log_side_effects` deletes the day's existing items and inserts the submitted
    list — the sheet always posts the full current selection. So an omitted
    effect is a removal, which is why the confirmation line says "replaces".
    """
    effects = _validated_effects(arguments.get("effects"))
    note = arguments.get("note")
    note = _text(note, "note", MAX_NOTE_CHARS) if note not in (None, "") else None

    rows = backend.log_side_effects(config, user_id, effects, note)
    logger.info("record_side_effects: %d effect(s) saved", len(effects))
    return {
        "status": "saved",
        "replaced_todays_set": True,
        "saved": [{"effect": row.get("effect"), "severity": row.get("severity")} for row in rows],
        "day": rows[0].get("log_date") if rows else None,
        "scale": "severity is 1 (mild) to 5 (severe) — higher is worse.",
    }


# ---------------------------------------------------------------------------
# To-dos
# ---------------------------------------------------------------------------


def set_todo(config: Settings, user_id: str, arguments: dict) -> dict:
    """Create a to-do, or edit the one named by `todo_id`.

    `upsert_todo` replaces every field, so an edit has to restate them all —
    the tool description says so, and retrieve_todos supplies the current values.
    """
    repeat_rule = _choice(arguments.get("repeat_rule"), "repeat_rule", TODO_REPEAT_RULES)
    due_date = iso_date(arguments.get("due_date"), "due_date")
    # The `todos_due_date_matches_repeat` check constraint, surfaced as a 400
    # the model can fix instead of a 500 from Postgres.
    if repeat_rule == "once" and not due_date:
        raise HTTPException(status_code=400, detail="A one-off to-do needs a due_date.")
    if repeat_rule == "daily" and due_date:
        raise HTTPException(status_code=400, detail="A daily to-do must not have a due_date.")

    entry = {
        "id": _todo_id(arguments, required=False),
        "title": _text(arguments.get("title"), "title", MAX_TITLE_CHARS),
        "category": _choice(arguments.get("category"), "category", TODO_CATEGORIES),
        "repeat_rule": repeat_rule,
        "remind_hour": _whole(arguments.get("remind_hour"), "remind_hour", 0, 23),
        "remind_minute": _whole(arguments.get("remind_minute"), "remind_minute", 0, 59),
        "due_date": due_date,
    }
    row = backend.upsert_todo(config, user_id, entry)
    logger.info("set_todo: %s", "edited" if entry["id"] else "created")
    return {
        "status": "saved",
        "created": entry["id"] is None,
        "todo": {"todo_id": row.get("id"), **{k: row.get(k) for k in ("title", "category")}},
    }


def complete_todo(config: Settings, user_id: str, arguments: dict) -> dict:
    """Tick or untick a to-do for the user's current day."""
    todo_id = _todo_id(arguments)
    done = arguments.get("done")
    done = True if done is None else bool(done)

    row = backend.set_todo_done(config, user_id, todo_id, done)
    logger.info("complete_todo: done=%s", done)
    return {
        "status": "saved",
        "todo": {
            "todo_id": row.get("id"),
            "title": row.get("title"),
            "is_done": row.get("is_done"),
        },
    }


def remove_todo(config: Settings, user_id: str, arguments: dict) -> dict:
    """Soft-delete a to-do. Irreversible from chat — hence the confirmation."""
    todo_id = _todo_id(arguments)
    backend.delete_todo(config, user_id, todo_id)
    logger.info("remove_todo: deleted")
    return {"status": "deleted", "todo_id": todo_id}
