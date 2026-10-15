"""Concurrent assembly of `GET /v1/dashboard`.

The dashboard is a fan-out of independent Supabase reads, so it runs in two
phases on a thread pool instead of one after another:

1. Everything that does not depend on the profile: `get_me`, weights, shots,
   side effects, the wellness summary and the logged-days set.
2. Once `get_me` is back we know the user's local day, so the two reads keyed
   on it (this week's nutrition days and the sleep check-ins) go out.

The response is identical to the sequential version: same keys, same order,
same fail-soft rules.

Every call goes through `backend.<name>` (never `from .backend import ...`)
so tests that monkeypatch the `backend` module still intercept these reads.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from fastapi import HTTPException

from . import backend
from .config import Settings

logger = logging.getLogger("scan.backend")

# Peak in-flight is 7 (6 phase-1 tasks, then 2 phase-2 once get_me is done);
# 8 matches main.py `_assemble` and food_search.py.
_MAX_WORKERS = 8


def _week_nutrition(config: Settings, user_id: str, week_ago: str) -> list:
    return backend._select(
        config,
        "nutrition_days",
        {
            "user_id": f"eq.{user_id}",
            "deleted_at": "is.null",
            "day": f"gte.{week_ago}",
            "select": "day,calories,protein_grams,carb_grams,fiber_grams,water_ounces",
            "order": "day.desc",
        },
    )


def _wellness_summary_soft(config: Settings, user_id: str) -> dict | None:
    # Fail soft: a pre-0003 database serves the dashboard without wellness.
    try:
        return backend.wellness_summary(config, user_id)
    except HTTPException:
        logger.warning("wellness summary unavailable; dashboard degrades without it")
        return None


def _logged_days_soft(config: Settings, user_id: str) -> set[str] | None:
    # Home's streak chip. Computed here rather than in SQL like wellness_streak
    # (0003) so it needs no migration; the rules are deliberately the same.
    # Fail soft: a streak is a garnish, never a reason to fail the dashboard.
    try:
        return backend._logged_nutrition_days(config, user_id)
    except HTTPException:
        logger.warning("nutrition streak unavailable; dashboard degrades without it")
        return None


def get_dashboard(config: Settings, user_id: str) -> dict:
    started = time.monotonic()
    # If any `.result()` below raises, leaving the `with` block waits for the
    # in-flight reads (bounded by `_http`'s 8s timeout) and the exception
    # propagates with its original status/detail.
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        me_f = pool.submit(backend.get_me, config, user_id)
        weights_f = pool.submit(backend.list_weights, config, user_id, 90)
        shots_f = pool.submit(backend.list_shots, config, user_id, 60)
        effects_f = pool.submit(backend.list_side_effects, config, user_id, 1)
        summary_f = pool.submit(_wellness_summary_soft, config, user_id)
        logged_f = pool.submit(_logged_days_soft, config, user_id)

        me = me_f.result()
        me_ms = int((time.monotonic() - started) * 1000)

        # "Today" is the user's local day — the same day the `log_*` SQL functions
        # stamp their rows with. Reading the server's own day drops every log made
        # while the two calendars disagree (an evening in the Americas, an early
        # morning east of UTC): the scan reports the calories, the dashboard then
        # shows zero.
        local_today = backend.profile_today(me["profile"])
        today = local_today.isoformat()
        week_ago = (local_today - timedelta(days=7)).isoformat()

        week_f = pool.submit(_week_nutrition, config, user_id, week_ago)
        sleep_f = pool.submit(backend.list_sleep_checkins, config, user_id, week_ago)

        week_nutrition = week_f.result()
        weights = weights_f.result()
        shots = shots_f.result()
        side_effects = effects_f.result()
        sleep_checkins = sleep_f.result()
        summary = summary_f.result()
        logged = logged_f.result()

    today_row = next((row for row in week_nutrition if row["day"] == today), None)

    effects_today = []
    for log in side_effects:
        if log["log_date"] == today:
            effects_today = log["effects"]

    wellness = None
    if summary:
        wellness = {
            "minutes_today": summary["minutes_today"],
            "streak_days": summary["streak_days"],
            "goal_minutes": me["nutrition_goals"].get("wellness_minutes_goal", 45),
        }

    streak_days = 0 if logged is None else backend.nutrition_streak(logged, local_today)

    total_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "dashboard user=%s me_ms=%d day_reads_ms=%d total_ms=%d",
        user_id,
        me_ms,
        total_ms - me_ms,
        total_ms,
    )

    # To-dos are deliberately NOT here: they are read and written through
    # /v1/todos, so carrying them would cost an extra RPC per dashboard call
    # for a payload no client decodes.
    return {
        "profile": me["profile"],
        "nutrition_goals": me["nutrition_goals"],
        "plan": me["plan"],
        "today": today_row,
        "week_nutrition": week_nutrition,
        "weights": weights,
        "shots": shots,
        "side_effects_today": effects_today,
        "sleep_checkins": sleep_checkins,
        "wellness": wellness,
        "streak_days": streak_days,
    }
