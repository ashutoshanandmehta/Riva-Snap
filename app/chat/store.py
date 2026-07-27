"""Chat persistence and the reads the handlers need.

Everything goes through `backend._rpc` / `backend._select` — the service-role
transport and its error mapping already exist and are not reimplemented here.
Writes hit the `SECURITY DEFINER` functions from `0005_chat.sql`, which re-check
thread ownership, so a forged `thread_id` writes nothing and surfaces as a 404.
"""

import logging
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from .. import backend
from ..config import Settings

logger = logging.getLogger("scan.chat.store")

THREAD_GONE = "That conversation no longer exists."

# Deliberately narrow: the identity columns on `profiles` (name, date_of_birth,
# clinician_name) are never read into a tool result, so they can never reach a
# third-party model. Only the numbers a companion needs to talk about progress.
_TARGET_COLUMNS = "start_weight,goal_weight,height_inches,timezone"

_DEFAULT_TIMEZONE = "America/New_York"

# The medication plan a companion can discuss. `id` is not exposed: nothing in a
# chat answer needs it, and it is not the client's to act on.
_PLAN_COLUMNS = "name,current_dose_mg,cadence_days,dose_frequency,start_date"

_NUTRITION_DAY_COLUMNS = "day,calories,protein_grams,carb_grams,fiber_grams,water_ounces"
_FOOD_ENTRY_COLUMNS = "day,scan_type,items,calories,protein_grams,water_ounces,created_at"
_NUTRITION_GOAL_COLUMNS = "protein_goal,carb_goal,fiber_goal,water_goal"
# Added by 0003; a pre-0003 sandbox lacks the column, so it is read separately
# and degrades to None rather than failing the whole wellness answer.
_WELLNESS_GOAL_COLUMN = "wellness_minutes_goal"
_HEALTH_GOAL_COLUMNS = (
    "glp1_support,weight_mgmt,nutrition_diet,muscle_preserve,exercise_move,sleep_recovery"
)
_WELLNESS_SESSION_COLUMNS = "day,practice_id,kind,minutes"

# One meal per scan; a month of eating is well under this. Bounds the payload so
# a wide date range cannot push a huge `items` blob into the model's context.
MAX_MEALS = 120


def validated_thread_id(thread_id: str) -> str:
    """A non-uuid thread id is an unknown conversation, not a server error."""
    try:
        uuid.UUID(thread_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=THREAD_GONE) from None
    return thread_id


def ensure_thread(config: Settings, user_id: str, thread_id: str | None) -> str:
    """Returns the thread to append to, opening one when the client sent none.

    Ownership of an existing thread is not checked here — the first
    `log_message` does it in SQL, which saves a round trip and keeps the check
    in one place.
    """
    if thread_id:
        return validated_thread_id(thread_id)
    rows = backend._rpc(config, "start_chat_thread", {"p_user_id": user_id, "p_title": None})
    if not rows:
        raise HTTPException(status_code=502, detail="Could not start the conversation. Try again.")
    return rows[0]["id"]


def log_message(
    config: Settings,
    user_id: str,
    thread_id: str,
    role: str,
    content: str,
    tool_calls: list[dict] | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
) -> dict:
    """Appends one turn. An empty result means the thread is not this user's."""
    rows = backend._rpc(
        config,
        "log_chat_message",
        {
            "p_user_id": user_id,
            "p_thread_id": thread_id,
            "p_role": role,
            "p_content": content,
            "p_tool_calls": tool_calls or [],
            "p_model": model,
            "p_prompt_version": prompt_version,
        },
    )
    if not rows:
        raise HTTPException(status_code=404, detail=THREAD_GONE)
    return rows[0]


def list_messages(config: Settings, user_id: str, thread_id: str, limit: int) -> list[dict]:
    """The thread's most recent `limit` turns, oldest-first."""
    return backend._rpc(
        config,
        "list_chat_messages",
        {"p_user_id": user_id, "p_thread_id": thread_id, "p_limit": limit},
    )


def list_threads(config: Settings, user_id: str, limit: int) -> list[dict]:
    return backend._rpc(config, "list_chat_threads", {"p_user_id": user_id, "p_limit": limit})


def delete_thread(config: Settings, user_id: str, thread_id: str) -> None:
    rows = backend._rpc(
        config, "delete_chat_thread", {"p_user_id": user_id, "p_thread_id": thread_id}
    )
    if not rows:
        raise HTTPException(status_code=404, detail=THREAD_GONE)


def profile_targets(config: Settings, user_id: str) -> dict:
    """Start weight, goal weight, height, timezone — nothing identifying.

    One select rather than `backend.get_me`, which fans out to four tables for
    data a weight answer does not need.
    """
    rows = backend._select(config, "profiles", {"id": f"eq.{user_id}", "select": _TARGET_COLUMNS})
    return rows[0] if rows else {}


def local_day(config: Settings, user_id: str) -> tuple[str, str]:
    """The user's own calendar day and timezone name.

    "Today" has to mean the user's local day, the same way every `log_*` function
    computes it in SQL from `profiles.timezone`. An unknown timezone falls back to
    the schema default rather than failing the read.
    """
    tz_name = (profile_targets(config, user_id).get("timezone") or "").strip()
    tz_name = tz_name or _DEFAULT_TIMEZONE
    try:
        now_local = datetime.now(ZoneInfo(tz_name))
    except (KeyError, ValueError):
        logger.warning("unknown profile timezone %r; falling back to UTC", tz_name)
        now_local = datetime.now(timezone.utc)
    return now_local.date().isoformat(), tz_name


def active_plan(config: Settings, user_id: str) -> dict | None:
    """The active medication plan, or None when the user has never set one."""
    rows = backend._select(
        config,
        "medication_plans",
        {
            "user_id": f"eq.{user_id}",
            "is_active": "eq.true",
            "deleted_at": "is.null",
            "select": _PLAN_COLUMNS,
            "limit": "1",
        },
    )
    return rows[0] if rows else None


def _day_range(column: str, since: str | None, until: str | None) -> dict:
    """PostgREST bounds for an inclusive ISO date range on a `date` column."""
    bounds = []
    if since:
        bounds.append(f"gte.{since}")
    if until:
        bounds.append(f"lte.{until}")
    return {column: bounds} if bounds else {}


def nutrition_days(
    config: Settings, user_id: str, since: str | None, until: str | None
) -> list[dict]:
    """Daily nutrition totals over an inclusive range, oldest day first.

    These rows are the same integers `log_scan` upserts, so a number quoted from
    here is exactly what the app's ring shows — no re-derivation from meals.
    """
    filters: dict = {
        "user_id": f"eq.{user_id}",
        "deleted_at": "is.null",
        "select": _NUTRITION_DAY_COLUMNS,
        "order": "day",
    }
    filters.update(_day_range("day", since, until))
    return backend._select(config, "nutrition_days", filters)


def food_entries(
    config: Settings, user_id: str, since: str | None, until: str | None
) -> list[dict]:
    """Individual logged meals over an inclusive range, oldest first.

    Date-ranged, unlike `backend.list_food_entries`, which is limit-only and
    serves the dashboard. That one is left alone.
    """
    filters: dict = {
        "user_id": f"eq.{user_id}",
        "deleted_at": "is.null",
        "select": _FOOD_ENTRY_COLUMNS,
        "order": "created_at",
        "limit": str(MAX_MEALS),
    }
    filters.update(_day_range("day", since, until))
    return backend._select(config, "food_entries", filters)


def nutrition_goals(config: Settings, user_id: str) -> dict:
    """The user's daily protein/carb/fibre/water targets."""
    rows = backend._select(
        config,
        "nutrition_goals",
        {"user_id": f"eq.{user_id}", "select": _NUTRITION_GOAL_COLUMNS},
    )
    return rows[0] if rows else {}


def wellness_minutes_goal(config: Settings, user_id: str) -> int | None:
    """The daily practice-minutes target, or None on a pre-0003 database."""
    try:
        rows = backend._select(
            config,
            "nutrition_goals",
            {"user_id": f"eq.{user_id}", "select": _WELLNESS_GOAL_COLUMN},
        )
    except HTTPException:
        logger.warning("nutrition_goals has no %s column; omitting", _WELLNESS_GOAL_COLUMN)
        return None
    return rows[0].get(_WELLNESS_GOAL_COLUMN) if rows else None


def health_goals(config: Settings, user_id: str) -> dict:
    """The six signup goal flags. Booleans only — nothing identifying."""
    rows = backend._select(
        config, "health_goals", {"user_id": f"eq.{user_id}", "select": _HEALTH_GOAL_COLUMNS}
    )
    return rows[0] if rows else {}


def wellness_sessions(
    config: Settings, user_id: str, since: str | None, until: str | None
) -> list[dict]:
    """Practice sessions over an inclusive range, oldest first.

    Date-ranged rather than the trailing-window `backend.list_wellness_sessions`,
    which the suggestion path owns and which anchors on the *server's* date.
    """
    filters: dict = {
        "user_id": f"eq.{user_id}",
        "deleted_at": "is.null",
        "select": _WELLNESS_SESSION_COLUMNS,
        "order": "day",
    }
    filters.update(_day_range("day", since, until))
    return backend._select(config, "wellness_sessions", filters)


def checkin_config(config: Settings) -> dict:
    """The global check-in question set and its options.

    These two tables are shared configuration, not user rows — no `user_id`
    filter applies. Options are grouped by question and ordered by `position`.
    """
    questions = backend._select(
        config,
        "checkin_questions",
        {
            "is_active": "eq.true",
            "select": "id,category,title,subtitle,symbol,sort_order",
            "order": "sort_order",
        },
    )
    options = backend._select(
        config,
        "checkin_options",
        {"select": "question_id,code,label,symbol,value,position", "order": "position"},
    )
    grouped: dict[str, list[dict]] = {}
    for option in options:
        grouped.setdefault(option["question_id"], []).append(option)
    return {"questions": questions, "options": grouped}


def checkin_answers(
    config: Settings,
    user_id: str,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """The user's check-in answers over an inclusive date range, joined to the
    option's label/value and the question's category.

    Joined in Python rather than through a PostgREST embed because
    `checkin_answers -> checkin_options` is a **composite** foreign key
    `(question_id, code)`. This mirrors `backend.list_sleep_checkins`, which is
    left untouched — the wellness suggestion path depends on it, and it only ever
    wanted the sleep question.
    """
    filters: dict = {
        "user_id": f"eq.{user_id}",
        "deleted_at": "is.null",
        "select": "id,checkin_date",
        "order": "checkin_date",
    }
    bounds = []
    if since:
        bounds.append(f"gte.{since}")
    if until:
        bounds.append(f"lte.{until}")
    if bounds:
        filters["checkin_date"] = bounds

    checkins = backend._select(config, "checkins", filters)
    if not checkins:
        return []

    day_by_id = {row["id"]: row["checkin_date"] for row in checkins}
    ids = ",".join(day_by_id)
    answers = backend._select(
        config,
        "checkin_answers",
        {
            "checkin_id": f"in.({ids})",
            "select": "checkin_id,question_id,option_code",
        },
    )
    if not answers:
        return []

    catalogue = checkin_config(config)
    category_by_question = {q["id"]: q.get("category") for q in catalogue["questions"]}
    title_by_question = {q["id"]: q.get("title") for q in catalogue["questions"]}
    option_by_key = {
        (question_id, option["code"]): option
        for question_id, options in catalogue["options"].items()
        for option in options
    }

    rows: list[dict] = []
    for answer in answers:
        question_id = answer["question_id"]
        option = option_by_key.get((question_id, answer["option_code"]))
        if option is None:
            # A retired option the config no longer describes. Its number would be
            # meaningless without a scale, so skip it rather than guess.
            logger.warning("check-in answer references unknown option for %s", question_id)
            continue
        rows.append(
            {
                "checkin_date": day_by_id[answer["checkin_id"]],
                "question_id": question_id,
                "category": category_by_question.get(question_id),
                "title": title_by_question.get(question_id),
                "option_code": answer["option_code"],
                "label": option.get("label"),
                "value": option.get("value"),
            }
        )
    rows.sort(key=lambda row: (row["checkin_date"], row["question_id"]))
    return rows
