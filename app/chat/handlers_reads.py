"""The second set of read handlers: nutrition, wellness, goals, to-dos.

`handlers.py` holds the original three (weight, check-ins, medical). Same
contract — `(config, user_id, arguments) -> JSON-safe dict` — and the same
principle: every average, streak and goal comparison is computed here, in
Python, and stated in a `summary` the model is told to quote rather than
recompute.
"""

import logging
from datetime import date, timedelta

from fastapi import HTTPException

from .. import backend
from ..config import Settings
from . import store

# Re-used rather than redefined: one window length and one date validator across
# every tool, so "recently" means the same thing whichever one the model picks.
from .handlers import DEFAULT_WINDOW_DAYS, _rounded, iso_date

logger = logging.getLogger("scan.chat.handlers_reads")

__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "retrieve_nutrition_log",
    "retrieve_profile_goals",
    "retrieve_todos",
    "retrieve_wellness_log",
]

# The nutrition columns that are summed, averaged, and goal-checked together.
_MACROS = ("calories", "protein_grams", "carb_grams", "fiber_grams", "water_ounces")

# Which daily goal, if any, each macro is measured against. Calories have no
# target row in `nutrition_goals`, so they are reported without one.
_GOAL_FOR_MACRO = {
    "protein_grams": "protein_goal",
    "carb_grams": "carb_goal",
    "fiber_grams": "fiber_goal",
    "water_ounces": "water_goal",
}

_HEALTH_GOAL_LABELS = {
    "glp1_support": "GLP-1 support",
    "weight_mgmt": "weight management",
    "nutrition_diet": "nutrition and diet",
    "muscle_preserve": "preserving muscle",
    "exercise_move": "exercise and movement",
    "sleep_recovery": "sleep and recovery",
}


def _window(config: Settings, user_id: str, arguments: dict) -> tuple[str, str]:
    """The requested range, defaulted to the last N days of the *user's* days.

    Anchored on the profile timezone the way every `log_*` function is, so a
    question asked at 11pm in Los Angeles does not silently read tomorrow.
    """
    start_date = iso_date(arguments.get("start_date"), "start_date")
    end_date = iso_date(arguments.get("end_date"), "end_date")
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date is after end_date.")
    if not start_date and not end_date:
        today, _ = store.local_day(config, user_id)
        end_date = today
        start_date = (date.fromisoformat(today) - timedelta(days=DEFAULT_WINDOW_DAYS)).isoformat()
    return start_date, end_date


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _item_names(items: object) -> list[str]:
    """Food names out of the stored `items` blob, defensively.

    The column is free-form jsonb written by the scan pipeline; a shape change
    there should cost a meal's item list, not the whole nutrition answer.
    """
    if not isinstance(items, list):
        return []
    names = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
        elif isinstance(item, str):
            names.append(item)
    return names


def retrieve_nutrition_log(config: Settings, user_id: str, arguments: dict) -> dict:
    """Daily nutrition totals against the user's goals, with the meals optional."""
    start_date, end_date = _window(config, user_id, arguments)

    rows = store.nutrition_days(config, user_id, start_date, end_date)
    entries = [{"day": row.get("day"), **{key: row.get(key) for key in _MACROS}} for row in rows]
    goals = store.nutrition_goals(config, user_id)

    summary: dict = {
        # Days with no scan have no row at all; this is a count of days that
        # actually hold data, not the length of the window.
        "days_logged": len(entries),
        "period_start": entries[0]["day"] if entries else None,
        "period_end": entries[-1]["day"] if entries else None,
        "latest_day": entries[-1] if entries else None,
    }
    for macro in _MACROS:
        values = [entry[macro] for entry in entries if isinstance(entry.get(macro), (int, float))]
        summary[f"avg_{macro}"] = _mean(values)
        summary[f"total_{macro}"] = round(sum(values)) if values else None
        goal_key = _GOAL_FOR_MACRO.get(macro)
        goal = goals.get(goal_key) if goal_key else None
        summary[f"days_meeting_{macro}_goal"] = (
            sum(1 for value in values if value >= goal) if goal else None
        )

    calorie_days = [entry for entry in entries if isinstance(entry.get("calories"), (int, float))]
    summary["highest_calorie_day"] = (
        max(calorie_days, key=lambda entry: entry["calories"]) if calorie_days else None
    )
    summary["lowest_calorie_day"] = (
        min(calorie_days, key=lambda entry: entry["calories"]) if calorie_days else None
    )

    result = {
        "requested_range": {"start_date": start_date, "end_date": end_date},
        "entries": entries,
        "goals": goals,
        "units": "calories kcal; protein/carb/fibre grams; water fluid ounces.",
        "summary": summary,
    }

    if arguments.get("include_meals"):
        meals = store.food_entries(config, user_id, start_date, end_date)
        result["meals"] = [
            {
                "day": meal.get("day"),
                "scan_type": meal.get("scan_type"),
                "logged_at": meal.get("created_at"),
                "calories": meal.get("calories"),
                "protein_grams": meal.get("protein_grams"),
                "water_ounces": meal.get("water_ounces"),
                "items": _item_names(meal.get("items")),
            }
            for meal in meals
        ]
        result["meals_truncated"] = len(meals) >= store.MAX_MEALS

    logger.info("retrieve_nutrition_log: %d days", len(entries))
    return result


def retrieve_wellness_log(config: Settings, user_id: str, arguments: dict) -> dict:
    """Practice sessions over a range, plus today's minutes and streak."""
    start_date, end_date = _window(config, user_id, arguments)

    rows = store.wellness_sessions(config, user_id, start_date, end_date)
    sessions = [
        {
            "day": row.get("day"),
            "kind": row.get("kind"),
            "practice_id": row.get("practice_id"),
            "minutes": row.get("minutes"),
        }
        for row in rows
    ]

    minutes_by_day: dict[str, int] = {}
    minutes_by_kind: dict[str, int] = {}
    for session in sessions:
        minutes = session["minutes"] or 0
        minutes_by_day[session["day"]] = minutes_by_day.get(session["day"], 0) + minutes
        kind = session["kind"] or "other"
        minutes_by_kind[kind] = minutes_by_kind.get(kind, 0) + minutes

    goal = store.wellness_minutes_goal(config, user_id)
    total_minutes = sum(minutes_by_day.values())
    # `wellness_summary` is the server's own streak arithmetic, in the user's
    # timezone. Never recomputed from the session rows, which may be windowed.
    today = backend.wellness_summary(config, user_id) or {}

    logger.info("retrieve_wellness_log: %d sessions", len(sessions))
    return {
        "requested_range": {"start_date": start_date, "end_date": end_date},
        "sessions": sessions,
        "today": {
            "day": today.get("day"),
            "minutes": today.get("minutes_today"),
            "streak_days": today.get("streak_days"),
        },
        "summary": {
            "session_count": len(sessions),
            "active_days": len(minutes_by_day),
            "total_minutes": total_minutes,
            "avg_minutes_per_active_day": _mean(list(minutes_by_day.values())),
            "minutes_by_kind": minutes_by_kind,
            "daily_minutes_goal": goal,
            "days_meeting_goal": (
                sum(1 for minutes in minutes_by_day.values() if minutes >= goal) if goal else None
            ),
        },
    }


def retrieve_profile_goals(config: Settings, user_id: str, arguments: dict) -> dict:
    """Targets and settings: weights, macros, practice minutes, plan, goal flags.

    Identity columns are never selected (see `store._TARGET_COLUMNS`), so a name
    or date of birth cannot reach the model through this path.
    """
    targets = store.profile_targets(config, user_id)
    flags = store.health_goals(config, user_id)

    logger.info("retrieve_profile_goals: plan=%s", bool(flags))
    return {
        "weight": {
            "start_weight_lbs": _rounded(targets.get("start_weight"), 2),
            "goal_weight_lbs": _rounded(targets.get("goal_weight"), 2),
            "height_inches": _rounded(targets.get("height_inches"), 1),
        },
        "nutrition_goals": store.nutrition_goals(config, user_id),
        "wellness_minutes_goal": store.wellness_minutes_goal(config, user_id),
        "health_goals": {
            "selected": [_HEALTH_GOAL_LABELS[key] for key in _HEALTH_GOAL_LABELS if flags.get(key)],
            "flags": flags,
        },
        "plan": store.active_plan(config, user_id),
        "timezone": targets.get("timezone"),
        "note": (
            "These are targets the user set, not measurements. For progress against"
            " them call retrieve_weight_log or retrieve_nutrition_log."
        ),
    }


def retrieve_todos(config: Settings, user_id: str, arguments: dict) -> dict:
    """Open to-dos with `is_done` already resolved for the user's own day."""
    rows = backend.list_todos(config, user_id)
    todos = [
        {
            "todo_id": row.get("id"),
            "title": row.get("title"),
            "category": row.get("category"),
            "repeat_rule": row.get("repeat_rule"),
            "remind_at": f"{row.get('remind_hour'):02d}:{row.get('remind_minute'):02d}"
            if row.get("remind_hour") is not None
            else None,
            "remind_hour": row.get("remind_hour"),
            "remind_minute": row.get("remind_minute"),
            "due_date": row.get("due_date"),
            "is_done": row.get("is_done"),
        }
        for row in rows
    ]
    day, tz_name = store.local_day(config, user_id)

    logger.info("retrieve_todos: %d open", len(todos))
    return {
        "day": day,
        "timezone": tz_name,
        "todos": todos,
        "open_count": sum(1 for todo in todos if not todo["is_done"]),
        "done_count": sum(1 for todo in todos if todo["is_done"]),
        "note": (
            "A daily to-do resets each morning; a one-off drops off the list once"
            " it is ticked. Use todo_id exactly as given — never invent one."
        ),
    }
