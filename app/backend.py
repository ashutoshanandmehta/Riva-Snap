"""Supabase integration: token verification and server-authoritative writes.

The client only ever authenticates (email OTP via supabase-js). All database
writes go through this module with the service role key, calling the
log_scan() Postgres function, which stamps the verified user id and updates
food_entries plus the nutrition_days daily aggregate in one transaction.
"""

import hashlib
import hmac
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import HTTPException

from .config import Settings

logger = logging.getLogger("scan.backend")

_http = httpx.Client(timeout=8.0)


def _service_headers(config: Settings) -> dict:
    key = config.supabase_service_role_key
    headers = {"apikey": key, "Content-Type": "application/json"}
    if key.startswith("eyJ"):
        # Legacy service_role keys are JWTs and also go in the Authorization
        # header. New sb_secret_ keys must not: they are not JWTs.
        headers["Authorization"] = f"Bearer {key}"
    return headers


def is_configured(config: Settings) -> bool:
    return bool(
        config.supabase_url and config.supabase_anon_key and config.supabase_service_role_key
    )


def verify_token(config: Settings, token: str) -> str:
    """Returns the user id for a valid Supabase access token, else 401."""
    try:
        response = _http.get(
            f"{config.supabase_url}/auth/v1/user",
            headers={
                "apikey": config.supabase_anon_key,
                "Authorization": f"Bearer {token}",
            },
        )
    except httpx.HTTPError as error:
        raise HTTPException(status_code=503, detail=f"Auth service unreachable: {error}") from error
    if response.status_code != 200:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    user_id = response.json().get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    return user_id


def device_session(config: Settings, device_id: str) -> dict:
    """Silently provisions (or reuses) a per-device account and returns a
    session for it. Interim identity while the product has no sign-in: the
    account's email is synthetic and its password is derived from the device
    id with the service role key, so only this server can compute it.
    """
    digest = hashlib.sha256(device_id.encode()).hexdigest()[:24]
    email = f"device-{digest}@devices.riva.app"
    password = hmac.new(
        config.supabase_service_role_key.encode(),
        f"riva-device:{device_id}".encode(),
        hashlib.sha256,
    ).hexdigest()

    def grant() -> httpx.Response:
        return _http.post(
            f"{config.supabase_url}/auth/v1/token?grant_type=password",
            headers={"apikey": config.supabase_anon_key, "Content-Type": "application/json"},
            json={"email": email, "password": password},
        )

    try:
        response = grant()
        if response.status_code != 200:
            created = _http.post(
                f"{config.supabase_url}/auth/v1/admin/users",
                headers=_service_headers(config),
                json={"email": email, "password": password, "email_confirm": True},
            )
            if created.status_code not in (200, 201):
                logger.error(
                    "device account create failed: %s %s",
                    created.status_code,
                    created.text[:300],
                )
                raise HTTPException(
                    status_code=502, detail="Could not set up this device. Try again."
                )
            response = grant()
    except httpx.HTTPError as error:
        raise HTTPException(status_code=503, detail=f"Auth service unreachable: {error}") from error

    if response.status_code != 200:
        logger.error("device grant failed: %s %s", response.status_code, response.text[:300])
        raise HTTPException(status_code=502, detail="Could not set up this device. Try again.")

    token = response.json()
    return {
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token"),
        "expires_at": token.get("expires_at"),
        "user_id": token["user"]["id"],
        "email": email,
    }


def _rpc(config: Settings, function: str, params: dict) -> list[dict]:
    """Calls a server-authoritative Postgres function with the service role."""
    try:
        response = _http.post(
            f"{config.supabase_url}/rest/v1/rpc/{function}",
            headers=_service_headers(config),
            json=params,
        )
    except httpx.HTTPError as error:
        raise HTTPException(status_code=503, detail=f"Backend unreachable: {error}") from error

    if response.status_code != 200:
        logger.error("%s RPC failed: %s %s", function, response.status_code, response.text[:300])
        raise HTTPException(status_code=502, detail="Could not save the log. Try again.")
    return response.json()


def log_scan(config: Settings, user_id: str, entry: dict) -> dict:
    """Persists an accepted scan via the log_scan RPC; returns day totals."""
    rows = _rpc(
        config,
        "log_scan",
        {
            "p_user_id": user_id,
            "p_scan_type": entry["scan_type"],
            "p_items": entry["items"],
            "p_calories": entry["calories"],
            "p_protein_grams": entry["protein_grams"],
            "p_carb_grams": entry["carb_grams"],
            "p_fiber_grams": entry["fiber_grams"],
            "p_water_ounces": entry["water_ounces"],
            "p_model": entry.get("model"),
            "p_prompt_version": entry.get("prompt_version"),
        },
    )
    if not rows:
        raise HTTPException(status_code=502, detail="Log saved but totals were not returned.")
    return rows[0]


def log_weight(config: Settings, user_id: str, pounds: float, measured_at: str | None) -> dict:
    rows = _rpc(
        config,
        "log_weight",
        {
            "p_user_id": user_id,
            "p_pounds": pounds,
            "p_measured_at": measured_at,
        },
    )
    if not rows:
        raise HTTPException(status_code=502, detail="Weight saved but was not returned.")
    return rows[0]


def log_shot(config: Settings, user_id: str, entry: dict) -> dict:
    rows = _rpc(
        config,
        "log_shot",
        {
            "p_user_id": user_id,
            "p_medication_name": entry["medication_name"],
            "p_dose_mg": entry["dose_mg"],
            "p_injection_site": entry["injection_site"],
            "p_comfort_rating": entry.get("comfort_rating"),
            "p_taken_at": entry.get("taken_at"),
        },
    )
    if not rows:
        raise HTTPException(status_code=502, detail="Shot saved but was not returned.")
    return rows[0]


def log_side_effects(
    config: Settings, user_id: str, effects: list[dict], note: str | None
) -> list[dict]:
    return _rpc(
        config,
        "log_side_effects",
        {
            "p_user_id": user_id,
            "p_effects": effects,
            "p_note": note,
        },
    )


def log_checkin(config: Settings, user_id: str, question_id: str, option_code: str) -> dict:
    rows = _rpc(
        config,
        "log_checkin",
        {
            "p_user_id": user_id,
            "p_question_id": question_id,
            "p_option_code": option_code,
        },
    )
    if not rows:
        raise HTTPException(status_code=502, detail="Answer saved but was not returned.")
    return rows[0]


def log_wellness_session(
    config: Settings, user_id: str, practice_id: str, kind: str, minutes: int
) -> dict:
    rows = _rpc(
        config,
        "log_wellness_session",
        {
            "p_user_id": user_id,
            "p_practice_id": practice_id,
            "p_kind": kind,
            "p_minutes": minutes,
        },
    )
    if not rows:
        raise HTTPException(status_code=502, detail="Session saved but was not returned.")
    return rows[0]


def wellness_summary(config: Settings, user_id: str) -> dict | None:
    """Today's minutes and streak (profile-timezone day), or None."""
    rows = _rpc(config, "wellness_summary", {"p_user_id": user_id})
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# To-dos: full CRUD, all through owned SECURITY DEFINER functions
# ---------------------------------------------------------------------------

_TODO_GONE = "That to-do no longer exists."


def list_todos(config: Settings, user_id: str) -> list[dict]:
    """Open to-dos with `is_done` already resolved for the profile day."""
    return _rpc(config, "list_todos", {"p_user_id": user_id})


def upsert_todo(config: Settings, user_id: str, entry: dict) -> dict:
    """Creates when `entry["id"]` is None, else edits that owned to-do."""
    rows = _rpc(
        config,
        "upsert_todo",
        {
            "p_user_id": user_id,
            "p_todo_id": entry.get("id"),
            "p_title": entry["title"],
            "p_category": entry["category"],
            "p_repeat_rule": entry["repeat_rule"],
            "p_remind_hour": entry["remind_hour"],
            "p_remind_minute": entry["remind_minute"],
            "p_due_date": entry.get("due_date"),
        },
    )
    if not rows:
        # An edit that matched nothing: the id is unknown or not this user's.
        raise HTTPException(status_code=404, detail=_TODO_GONE)
    return rows[0]


def set_todo_done(config: Settings, user_id: str, todo_id: str, done: bool) -> dict:
    rows = _rpc(
        config,
        "set_todo_done",
        {"p_user_id": user_id, "p_todo_id": todo_id, "p_done": done},
    )
    if not rows:
        raise HTTPException(status_code=404, detail=_TODO_GONE)
    return rows[0]


def delete_todo(config: Settings, user_id: str, todo_id: str) -> None:
    rows = _rpc(config, "delete_todo", {"p_user_id": user_id, "p_todo_id": todo_id})
    if not rows:
        raise HTTPException(status_code=404, detail=_TODO_GONE)


# ---------------------------------------------------------------------------
# Reads and updates: profile, goals, plan, histories, export, account
# ---------------------------------------------------------------------------

_LOAD_DETAIL = "Could not load your data. Try again."
_SAVE_DETAIL = "Could not save your changes. Try again."

# Matches the `profiles.timezone` column default and the `log_*` functions.
_DEFAULT_TZ = "America/New_York"
_MIGRATION_DETAIL = (
    "The database is missing tables from a migration. Run the unapplied files in "
    "backend/supabase/migrations/ in the Supabase SQL Editor."
)

_PROFILE_COLUMNS = (
    "name,date_of_birth,gender,clinician_name,start_weight,goal_weight,height_inches,timezone"
)
_GOAL_COLUMNS = "protein_goal,carb_goal,fiber_goal,water_goal,wellness_minutes_goal"
# Pre-0003 databases lack wellness_minutes_goal; used to degrade that field alone.
_GOAL_COLUMNS_LEGACY = "protein_goal,carb_goal,fiber_goal,water_goal"
_HEALTH_GOAL_COLUMNS = (
    "glp1_support,weight_mgmt,nutrition_diet,muscle_preserve,exercise_move,"
    "sleep_recovery,weight_maintain"
)
_PLAN_COLUMNS = "name,current_dose_mg,cadence_days,dose_frequency,reminder_description,start_date"

PROFILE_FIELDS = (
    "name",
    "date_of_birth",
    "gender",
    "clinician_name",
    "start_weight",
    "goal_weight",
    "height_inches",
    "timezone",
)
GOAL_FIELDS = ("protein_goal", "carb_goal", "fiber_goal", "water_goal", "wellness_minutes_goal")
PLAN_FIELDS = ("name", "current_dose_mg", "cadence_days", "reminder_description")

_PLAN_DEFAULTS = {"name": "Semaglutide", "current_dose_mg": 0.5, "cadence_days": 7}


def _postgrest_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get("code") if isinstance(body, dict) else None


def _rest(
    config: Settings,
    method: str,
    table: str,
    params: dict | None,
    payload: dict | None,
    error_detail: str,
) -> list:
    """One PostgREST table call with the service role; returns parsed rows."""
    headers = _service_headers(config)
    if payload is not None:
        headers["Prefer"] = "return=representation"
    try:
        response = _http.request(
            method,
            f"{config.supabase_url}/rest/v1/{table}",
            headers=headers,
            params=params,
            json=payload,
        )
    except httpx.HTTPError as error:
        raise HTTPException(status_code=503, detail=f"Backend unreachable: {error}") from error
    if not 200 <= response.status_code < 300:
        logger.error(
            "%s %s failed: %s %s", method, table, response.status_code, response.text[:300]
        )
        if response.status_code == 404 and _postgrest_code(response) == "PGRST205":
            raise HTTPException(status_code=502, detail=_MIGRATION_DETAIL)
        raise HTTPException(status_code=502, detail=error_detail)
    return response.json()


def _select(config: Settings, table: str, params: dict) -> list:
    return _rest(config, "GET", table, params, None, _LOAD_DETAIL)


def _patch(config: Settings, table: str, filters: dict, payload: dict) -> list:
    return _rest(config, "PATCH", table, filters, payload, _SAVE_DETAIL)


def _insert(config: Settings, table: str, payload: dict, params: dict | None = None) -> list:
    return _rest(config, "POST", table, params, payload, _SAVE_DETAIL)


def get_me(config: Settings, user_id: str) -> dict:
    """Profile row, goals, health goal flags, and the active plan (or None)."""
    profiles = _select(config, "profiles", {"id": f"eq.{user_id}", "select": _PROFILE_COLUMNS})
    try:
        goals = _select(
            config, "nutrition_goals", {"user_id": f"eq.{user_id}", "select": _GOAL_COLUMNS}
        )
    except HTTPException:
        # Pre-0003 DBs lack wellness_minutes_goal (undefined_column 42703). Degrade
        # just that field with a default so /v1/me, /v1/dashboard, /v1/goals still work.
        goals = _select(
            config, "nutrition_goals", {"user_id": f"eq.{user_id}", "select": _GOAL_COLUMNS_LEGACY}
        )
        for row in goals:
            row.setdefault("wellness_minutes_goal", 45)
    health = _select(
        config, "health_goals", {"user_id": f"eq.{user_id}", "select": _HEALTH_GOAL_COLUMNS}
    )
    plans = _select(
        config,
        "medication_plans",
        {
            "user_id": f"eq.{user_id}",
            "is_active": "eq.true",
            "select": _PLAN_COLUMNS,
            "limit": "1",
        },
    )
    if not (profiles and goals and health):
        # The signup trigger provisions these rows, so a miss is a broken account.
        logger.error("account rows missing for user %s", user_id)
        raise HTTPException(status_code=502, detail=_LOAD_DETAIL)
    return {
        "profile": profiles[0],
        "nutrition_goals": goals[0],
        "health_goals": health[0],
        "plan": plans[0] if plans else None,
    }


def update_profile(config: Settings, user_id: str, fields: dict) -> dict:
    payload = {key: fields[key] for key in PROFILE_FIELDS if key in fields}
    if payload:
        rows = _patch(
            config, "profiles", {"id": f"eq.{user_id}", "select": _PROFILE_COLUMNS}, payload
        )
    else:
        rows = _select(config, "profiles", {"id": f"eq.{user_id}", "select": _PROFILE_COLUMNS})
    if not rows:
        raise HTTPException(status_code=502, detail=_SAVE_DETAIL)
    return rows[0]


def update_goals(config: Settings, user_id: str, fields: dict) -> dict:
    payload = {key: fields[key] for key in GOAL_FIELDS if key in fields}
    if payload:
        rows = _patch(
            config,
            "nutrition_goals",
            {"user_id": f"eq.{user_id}", "select": _GOAL_COLUMNS},
            payload,
        )
    else:
        rows = _select(
            config, "nutrition_goals", {"user_id": f"eq.{user_id}", "select": _GOAL_COLUMNS}
        )
    if not rows:
        raise HTTPException(status_code=502, detail=_SAVE_DETAIL)
    return rows[0]


HEALTH_GOAL_FIELDS = (
    "glp1_support",
    "weight_mgmt",
    "nutrition_diet",
    "muscle_preserve",
    "exercise_move",
    "sleep_recovery",
)


def update_health_goals(config: Settings, user_id: str, fields: dict) -> dict:
    payload = {key: fields[key] for key in HEALTH_GOAL_FIELDS if key in fields}
    if payload:
        rows = _patch(
            config,
            "health_goals",
            {"user_id": f"eq.{user_id}", "select": _HEALTH_GOAL_COLUMNS},
            payload,
        )
    else:
        rows = _select(
            config, "health_goals", {"user_id": f"eq.{user_id}", "select": _HEALTH_GOAL_COLUMNS}
        )
    if not rows:
        raise HTTPException(status_code=502, detail=_SAVE_DETAIL)
    return rows[0]


def upsert_plan(config: Settings, user_id: str, fields: dict) -> dict:
    """Updates the active medication plan, creating one on first use."""
    payload = {key: fields[key] for key in PLAN_FIELDS if key in fields}
    active = _select(
        config,
        "medication_plans",
        {
            "user_id": f"eq.{user_id}",
            "is_active": "eq.true",
            "select": "id," + _PLAN_COLUMNS,
            "limit": "1",
        },
    )
    if active:
        if not payload:
            plan = dict(active[0])
            plan.pop("id", None)
            return plan
        rows = _patch(
            config,
            "medication_plans",
            {"id": f"eq.{active[0]['id']}", "select": _PLAN_COLUMNS},
            payload,
        )
    else:
        rows = _insert(
            config,
            "medication_plans",
            {"user_id": user_id, **_PLAN_DEFAULTS, **payload},
            params={"select": _PLAN_COLUMNS},
        )
    if not rows:
        raise HTTPException(status_code=502, detail=_SAVE_DETAIL)
    return rows[0]


def _timestamp_day_bounds(since: str | None, until: str | None) -> list[str]:
    """PostgREST filters for an inclusive ISO **date** range over a timestamptz
    column. `until` becomes an exclusive `< next day` so the whole end day is
    covered whatever time the row was stamped. Returned as a list because
    PostgREST takes two bounds as a repeated query parameter."""
    bounds: list[str] = []
    if since:
        bounds.append(f"gte.{since}")
    if until:
        bounds.append(f"lt.{(date.fromisoformat(until) + timedelta(days=1)).isoformat()}")
    return bounds


def list_weights(
    config: Settings,
    user_id: str,
    limit: int,
    since: str | None = None,
    until: str | None = None,
) -> list:
    """Weight history, newest first. `since`/`until` are inclusive ISO dates;
    omitting both keeps the original limit-only behaviour."""
    params: dict = {
        "user_id": f"eq.{user_id}",
        "deleted_at": "is.null",
        "select": "id,pounds,dose_mg,measured_at",
        "order": "measured_at.desc",
        "limit": str(limit),
    }
    bounds = _timestamp_day_bounds(since, until)
    if bounds:
        params["measured_at"] = bounds
    return _select(config, "weights", params)


def list_food_entries(config: Settings, user_id: str, limit: int | None = None) -> list[dict]:
    params = {
        "user_id": f"eq.{user_id}",
        "deleted_at": "is.null",
        "select": "day,scan_type,items,calories,protein_grams,water_ounces,created_at",
        "order": "created_at.desc",
    }
    if limit is not None:
        params["limit"] = str(limit)
    return _select(config, "food_entries", params)


def list_shots(
    config: Settings,
    user_id: str,
    limit: int,
    since: str | None = None,
    until: str | None = None,
) -> list:
    """Shot history, newest first. `since`/`until` are inclusive ISO dates;
    omitting both keeps the original limit-only behaviour."""
    params: dict = {
        "user_id": f"eq.{user_id}",
        "deleted_at": "is.null",
        "select": "id,medication_name,dose_mg,taken_at,injection_site,comfort_rating",
        "order": "taken_at.desc",
        "limit": str(limit),
    }
    bounds = _timestamp_day_bounds(since, until)
    if bounds:
        params["taken_at"] = bounds
    return _select(config, "shots", params)


def list_wellness_sessions(config: Settings, user_id: str, days: int) -> list:
    """Recent sessions, newest first, for the suggestion context."""
    since = (date.today() - timedelta(days=days)).isoformat()
    return _select(
        config,
        "wellness_sessions",
        {
            "user_id": f"eq.{user_id}",
            "deleted_at": "is.null",
            "day": f"gte.{since}",
            "select": "day,practice_id,kind,minutes",
            "order": "day.desc",
        },
    )


def get_cached_suggestions(config: Settings, user_id: str, day: str) -> dict | None:
    """The user's cached suggestion payload for the day, or None."""
    rows = _select(
        config,
        "wellness_suggestions",
        {
            "user_id": f"eq.{user_id}",
            "day": f"eq.{day}",
            "select": "payload",
            "limit": "1",
        },
    )
    return rows[0]["payload"] if rows else None


def cache_suggestions(config: Settings, user_id: str, day: str, payload: dict) -> None:
    _insert(
        config,
        "wellness_suggestions",
        {
            "user_id": user_id,
            "day": day,
            "payload": payload,
        },
    )


def list_side_effects(
    config: Settings,
    user_id: str,
    days: int,
    since: str | None = None,
    until: str | None = None,
) -> list:
    """Daily logs for the window, each with its effects, newest first.

    `since`/`until` are inclusive ISO dates that override the rolling `days`
    window; `log_date` is a date column, so both bounds apply directly."""
    bounds = [f"gte.{since or (date.today() - timedelta(days=days)).isoformat()}"]
    if until:
        bounds.append(f"lte.{until}")
    logs = _select(
        config,
        "side_effect_logs",
        {
            "user_id": f"eq.{user_id}",
            "deleted_at": "is.null",
            "log_date": bounds,
            "select": "id,log_date,note",
            "order": "log_date.desc",
        },
    )
    if not logs:
        return []
    log_ids = ",".join(log["id"] for log in logs)
    items = _select(
        config,
        "side_effect_log_items",
        {
            "log_id": f"in.({log_ids})",
            "select": "log_id,effect,severity",
        },
    )
    by_log: dict[str, list] = {}
    for item in items:
        by_log.setdefault(item["log_id"], []).append(
            {"effect": item["effect"], "severity": item["severity"]}
        )
    return [
        {"log_date": log["log_date"], "note": log["note"], "effects": by_log.get(log["id"], [])}
        for log in logs
    ]


def list_sleep_checkins(config: Settings, user_id: str, since: str) -> list[dict]:
    """Sleep-quality check-ins since the date, newest first (also feeds the
    wellness suggestion context)."""
    sleep_checkins: list[dict] = []
    checkins = _select(
        config,
        "checkins",
        {
            "user_id": f"eq.{user_id}",
            "deleted_at": "is.null",
            "checkin_date": f"gte.{since}",
            "select": "id,checkin_date",
            "order": "checkin_date.desc",
        },
    )
    if checkins:
        ids = ",".join(row["id"] for row in checkins)
        answers = _select(
            config,
            "checkin_answers",
            {
                "checkin_id": f"in.({ids})",
                "question_id": "eq.sleep",
                "select": "checkin_id,option_code",
            },
        )
        options = _select(
            config,
            "checkin_options",
            {
                "question_id": "eq.sleep",
                "select": "code,label,value",
            },
        )
        by_code = {opt["code"]: opt for opt in options}
        by_checkin = {a["checkin_id"]: a["option_code"] for a in answers}
        for row in checkins:
            code = by_checkin.get(row["id"])
            option = by_code.get(code or "")
            if option:
                sleep_checkins.append(
                    {
                        "checkin_date": row["checkin_date"],
                        "value": option["value"],
                        "label": option["label"],
                    }
                )
    return sleep_checkins


def profile_today(profile: dict) -> date:
    """The user's local calendar day, matching what the `log_*` functions
    compute in SQL. An unknown timezone falls back to the server's day rather
    than failing the whole dashboard."""
    try:
        return datetime.now(ZoneInfo(profile.get("timezone") or _DEFAULT_TZ)).date()
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("unknown profile timezone %r; using server day", profile.get("timezone"))
        return date.today()


def nutrition_streak(logged_days: set[str], today: date) -> int:
    """Consecutive days with nutrition logged, ending today or yesterday.

    Mirrors `wellness_streak` + `wellness_summary` (0003): an unbroken streak
    survives until the end of the current day, so a day with nothing logged
    *yet* does not zero it.
    """
    anchor = today if today.isoformat() in logged_days else today - timedelta(days=1)
    streak = 0
    while (anchor - timedelta(days=streak)).isoformat() in logged_days:
        streak += 1
    return streak


def _logged_nutrition_days(config: Settings, user_id: str) -> set[str]:
    """Every day this user has real nutrition activity. Capped at 400 rows —
    past thirteen months the number is decorative, and the cap keeps one
    dashboard call from pulling an unbounded history.

    A `nutrition_days` row exists as soon as anything touches the day, so an
    all-zero row is not a logged day. The filter runs here rather than as a
    PostgREST `or=` so the query stays one this module already knows works.
    """
    rows = _select(
        config,
        "nutrition_days",
        {
            "user_id": f"eq.{user_id}",
            "deleted_at": "is.null",
            "select": "day,calories,water_ounces",
            "order": "day.desc",
            "limit": "400",
        },
    )
    return {
        row["day"]
        for row in rows
        if (row.get("calories") or 0) > 0 or (row.get("water_ounces") or 0) > 0
    }


def get_dashboard(config: Settings, user_id: str) -> dict:
    """Everything the app's dashboards need in one round trip: profile,
    goals, plan, this week's nutrition days, weight and shot history,
    today's side effects, the week's sleep check-ins, and the wellness
    summary."""
    me = get_me(config, user_id)
    # "Today" is the user's local day — the same day the `log_*` SQL functions
    # stamp their rows with. Reading the server's own day drops every log made
    # while the two calendars disagree (an evening in the Americas, an early
    # morning east of UTC): the scan reports the calories, the dashboard then
    # shows zero.
    local_today = profile_today(me["profile"])
    today = local_today.isoformat()
    week_ago = (local_today - timedelta(days=7)).isoformat()

    week_nutrition = _select(
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
    today_row = next((row for row in week_nutrition if row["day"] == today), None)

    weights = list_weights(config, user_id, 90)
    shots = list_shots(config, user_id, 60)

    effects_today = []
    for log in list_side_effects(config, user_id, 1):
        if log["log_date"] == today:
            effects_today = log["effects"]

    sleep_checkins = list_sleep_checkins(config, user_id, week_ago)

    # Fail soft: a pre-0003 database serves the dashboard without wellness.
    wellness = None
    try:
        summary = wellness_summary(config, user_id)
        if summary:
            wellness = {
                "minutes_today": summary["minutes_today"],
                "streak_days": summary["streak_days"],
                "goal_minutes": me["nutrition_goals"].get("wellness_minutes_goal", 45),
            }
    except HTTPException:
        logger.warning("wellness summary unavailable; dashboard degrades without it")

    # Home's streak chip. Computed here rather than in SQL like wellness_streak
    # (0003) so it needs no migration; the rules are deliberately the same.
    # Fail soft: a streak is a garnish, never a reason to fail the dashboard.
    try:
        streak_days = nutrition_streak(_logged_nutrition_days(config, user_id), local_today)
    except HTTPException:
        logger.warning("nutrition streak unavailable; dashboard degrades without it")
        streak_days = 0

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


def export_user(config: Settings, user_id: str) -> dict:
    """Every row the user owns, soft-deleted included; it is their export."""
    owned = {"user_id": f"eq.{user_id}", "select": "*"}
    profiles = _select(config, "profiles", {"id": f"eq.{user_id}", "select": "*"})
    goals = _select(config, "nutrition_goals", dict(owned))
    health = _select(config, "health_goals", dict(owned))

    logs = _select(config, "side_effect_logs", {**owned, "order": "log_date.desc"})
    items_by_log: dict[str, list] = {}
    for item in _select(config, "side_effect_log_items", dict(owned)):
        items_by_log.setdefault(item["log_id"], []).append(item)
    for log in logs:
        log["items"] = items_by_log.get(log["id"], [])

    checkins = _select(config, "checkins", {**owned, "order": "checkin_date.desc"})
    answers_by_checkin: dict[str, list] = {}
    for answer in _select(config, "checkin_answers", dict(owned)):
        answers_by_checkin.setdefault(answer["checkin_id"], []).append(answer)
    for checkin in checkins:
        checkin["answers"] = answers_by_checkin.get(checkin["id"], [])

    try:
        wellness_sessions = _select(config, "wellness_sessions", {**owned, "order": "day.desc"})
    except HTTPException:
        # Pre-0003 DBs lack the wellness_sessions table; degrade that section only
        # rather than failing the whole privacy export.
        wellness_sessions = []

    try:
        todos = _select(config, "todos", {**owned, "order": "created_at.desc"})
    except HTTPException:
        # Pre-0004 DBs lack the todos table; same single-section degrade.
        todos = []

    return {
        "profile": profiles[0] if profiles else None,
        "nutrition_goals": goals[0] if goals else None,
        "health_goals": health[0] if health else None,
        "plans": _select(config, "medication_plans", {**owned, "order": "created_at.desc"}),
        "weights": _select(config, "weights", {**owned, "order": "measured_at.desc"}),
        "shots": _select(config, "shots", {**owned, "order": "taken_at.desc"}),
        "nutrition_days": _select(config, "nutrition_days", {**owned, "order": "day.desc"}),
        "food_entries": _select(config, "food_entries", {**owned, "order": "created_at.desc"}),
        "side_effect_logs": logs,
        "checkins": checkins,
        "wellness_sessions": wellness_sessions,
        "todos": todos,
    }


def delete_account(config: Settings, user_id: str) -> None:
    """Deletes the auth user via the GoTrue admin API; rows cascade."""
    try:
        response = _http.delete(
            f"{config.supabase_url}/auth/v1/admin/users/{user_id}",
            headers=_service_headers(config),
        )
    except httpx.HTTPError as error:
        raise HTTPException(status_code=503, detail=f"Auth service unreachable: {error}") from error
    if not 200 <= response.status_code < 300:
        logger.error("account delete failed: %s %s", response.status_code, response.text[:300])
        raise HTTPException(status_code=502, detail="Could not delete the account. Try again.")
