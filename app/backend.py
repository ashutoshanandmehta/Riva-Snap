"""Supabase integration: token verification and server-authoritative writes.

The client only ever authenticates (email OTP via supabase-js). All database
writes go through this module with the service role key, calling the
log_scan() Postgres function, which stamps the verified user id and updates
food_entries plus the nutrition_days daily aggregate in one transaction.
"""

import hashlib
import hmac
import logging
from datetime import date, timedelta

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
        config.supabase_url
        and config.supabase_anon_key
        and config.supabase_service_role_key
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
                    created.status_code, created.text[:300],
                )
                raise HTTPException(status_code=502, detail="Could not set up this device. Try again.")
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
    rows = _rpc(config, "log_scan", {
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
    })
    if not rows:
        raise HTTPException(status_code=502, detail="Log saved but totals were not returned.")
    return rows[0]


def log_weight(config: Settings, user_id: str, pounds: float, measured_at: str | None) -> dict:
    rows = _rpc(config, "log_weight", {
        "p_user_id": user_id,
        "p_pounds": pounds,
        "p_measured_at": measured_at,
    })
    if not rows:
        raise HTTPException(status_code=502, detail="Weight saved but was not returned.")
    return rows[0]


def log_shot(config: Settings, user_id: str, entry: dict) -> dict:
    rows = _rpc(config, "log_shot", {
        "p_user_id": user_id,
        "p_medication_name": entry["medication_name"],
        "p_dose_mg": entry["dose_mg"],
        "p_injection_site": entry["injection_site"],
        "p_comfort_rating": entry.get("comfort_rating"),
        "p_taken_at": entry.get("taken_at"),
    })
    if not rows:
        raise HTTPException(status_code=502, detail="Shot saved but was not returned.")
    return rows[0]


def log_side_effects(config: Settings, user_id: str, effects: list[dict], note: str | None) -> list[dict]:
    return _rpc(config, "log_side_effects", {
        "p_user_id": user_id,
        "p_effects": effects,
        "p_note": note,
    })


def log_checkin(config: Settings, user_id: str, question_id: str, option_code: str) -> dict:
    rows = _rpc(config, "log_checkin", {
        "p_user_id": user_id,
        "p_question_id": question_id,
        "p_option_code": option_code,
    })
    if not rows:
        raise HTTPException(status_code=502, detail="Answer saved but was not returned.")
    return rows[0]


# ---------------------------------------------------------------------------
# Reads and updates: profile, goals, plan, histories, export, account
# ---------------------------------------------------------------------------

_LOAD_DETAIL = "Could not load your data. Try again."
_SAVE_DETAIL = "Could not save your changes. Try again."
_MIGRATION_DETAIL = (
    "The database is missing tables from migration 0002. Run "
    "backend/supabase/migrations/0002_logging.sql in the Supabase SQL Editor."
)

_PROFILE_COLUMNS = (
    "name,date_of_birth,gender,clinician_name,"
    "start_weight,goal_weight,height_inches,timezone"
)
_GOAL_COLUMNS = "protein_goal,carb_goal,fiber_goal,water_goal"
_HEALTH_GOAL_COLUMNS = (
    "glp1_support,weight_mgmt,nutrition_diet,muscle_preserve,exercise_move,sleep_recovery"
)
_PLAN_COLUMNS = (
    "name,current_dose_mg,cadence_days,dose_frequency,reminder_description,start_date"
)

PROFILE_FIELDS = (
    "name", "date_of_birth", "gender", "clinician_name",
    "start_weight", "goal_weight", "height_inches", "timezone",
)
GOAL_FIELDS = ("protein_goal", "carb_goal", "fiber_goal", "water_goal")
PLAN_FIELDS = ("name", "current_dose_mg", "cadence_days", "reminder_description")

_PLAN_DEFAULTS = {"name": "Semaglutide", "current_dose_mg": 0.5, "cadence_days": 7}


def _postgrest_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get("code") if isinstance(body, dict) else None


def _rest(config: Settings, method: str, table: str, params: dict | None,
          payload: dict | None, error_detail: str) -> list:
    """One PostgREST table call with the service role; returns parsed rows."""
    headers = _service_headers(config)
    if payload is not None:
        headers["Prefer"] = "return=representation"
    try:
        response = _http.request(
            method, f"{config.supabase_url}/rest/v1/{table}",
            headers=headers, params=params, json=payload,
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
    goals = _select(config, "nutrition_goals", {"user_id": f"eq.{user_id}", "select": _GOAL_COLUMNS})
    health = _select(config, "health_goals", {"user_id": f"eq.{user_id}", "select": _HEALTH_GOAL_COLUMNS})
    plans = _select(config, "medication_plans", {
        "user_id": f"eq.{user_id}", "is_active": "eq.true",
        "select": _PLAN_COLUMNS, "limit": "1",
    })
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
        rows = _patch(config, "profiles", {"id": f"eq.{user_id}", "select": _PROFILE_COLUMNS}, payload)
    else:
        rows = _select(config, "profiles", {"id": f"eq.{user_id}", "select": _PROFILE_COLUMNS})
    if not rows:
        raise HTTPException(status_code=502, detail=_SAVE_DETAIL)
    return rows[0]


def update_goals(config: Settings, user_id: str, fields: dict) -> dict:
    payload = {key: fields[key] for key in GOAL_FIELDS if key in fields}
    if payload:
        rows = _patch(config, "nutrition_goals", {"user_id": f"eq.{user_id}", "select": _GOAL_COLUMNS}, payload)
    else:
        rows = _select(config, "nutrition_goals", {"user_id": f"eq.{user_id}", "select": _GOAL_COLUMNS})
    if not rows:
        raise HTTPException(status_code=502, detail=_SAVE_DETAIL)
    return rows[0]


HEALTH_GOAL_FIELDS = (
    "glp1_support", "weight_mgmt", "nutrition_diet",
    "muscle_preserve", "exercise_move", "sleep_recovery",
)


def update_health_goals(config: Settings, user_id: str, fields: dict) -> dict:
    payload = {key: fields[key] for key in HEALTH_GOAL_FIELDS if key in fields}
    if payload:
        rows = _patch(config, "health_goals", {"user_id": f"eq.{user_id}", "select": _HEALTH_GOAL_COLUMNS}, payload)
    else:
        rows = _select(config, "health_goals", {"user_id": f"eq.{user_id}", "select": _HEALTH_GOAL_COLUMNS})
    if not rows:
        raise HTTPException(status_code=502, detail=_SAVE_DETAIL)
    return rows[0]


def upsert_plan(config: Settings, user_id: str, fields: dict) -> dict:
    """Updates the active medication plan, creating one on first use."""
    payload = {key: fields[key] for key in PLAN_FIELDS if key in fields}
    active = _select(config, "medication_plans", {
        "user_id": f"eq.{user_id}", "is_active": "eq.true",
        "select": "id," + _PLAN_COLUMNS, "limit": "1",
    })
    if active:
        if not payload:
            plan = dict(active[0])
            plan.pop("id", None)
            return plan
        rows = _patch(config, "medication_plans",
                      {"id": f"eq.{active[0]['id']}", "select": _PLAN_COLUMNS}, payload)
    else:
        rows = _insert(config, "medication_plans",
                       {"user_id": user_id, **_PLAN_DEFAULTS, **payload},
                       params={"select": _PLAN_COLUMNS})
    if not rows:
        raise HTTPException(status_code=502, detail=_SAVE_DETAIL)
    return rows[0]


def list_weights(config: Settings, user_id: str, limit: int) -> list:
    return _select(config, "weights", {
        "user_id": f"eq.{user_id}", "deleted_at": "is.null",
        "select": "id,pounds,dose_mg,measured_at",
        "order": "measured_at.desc", "limit": str(limit),
    })


def list_shots(config: Settings, user_id: str, limit: int) -> list:
    return _select(config, "shots", {
        "user_id": f"eq.{user_id}", "deleted_at": "is.null",
        "select": "id,medication_name,dose_mg,taken_at,injection_site,comfort_rating",
        "order": "taken_at.desc", "limit": str(limit),
    })


def list_side_effects(config: Settings, user_id: str, days: int) -> list:
    """Daily logs for the window, each with its effects, newest first."""
    since = (date.today() - timedelta(days=days)).isoformat()
    logs = _select(config, "side_effect_logs", {
        "user_id": f"eq.{user_id}", "deleted_at": "is.null",
        "log_date": f"gte.{since}",
        "select": "id,log_date,note", "order": "log_date.desc",
    })
    if not logs:
        return []
    log_ids = ",".join(log["id"] for log in logs)
    items = _select(config, "side_effect_log_items", {
        "log_id": f"in.({log_ids})", "select": "log_id,effect,severity",
    })
    by_log: dict[str, list] = {}
    for item in items:
        by_log.setdefault(item["log_id"], []).append(
            {"effect": item["effect"], "severity": item["severity"]}
        )
    return [
        {"log_date": log["log_date"], "note": log["note"], "effects": by_log.get(log["id"], [])}
        for log in logs
    ]


def get_dashboard(config: Settings, user_id: str) -> dict:
    """Everything the app's dashboards need in one round trip: profile,
    goals, plan, this week's nutrition days, weight and shot history,
    today's side effects, and the week's sleep check-ins."""
    me = get_me(config, user_id)
    today = date.today().isoformat()
    week_ago = (date.today() - timedelta(days=7)).isoformat()

    week_nutrition = _select(config, "nutrition_days", {
        "user_id": f"eq.{user_id}", "deleted_at": "is.null",
        "day": f"gte.{week_ago}",
        "select": "day,calories,protein_grams,carb_grams,fiber_grams,water_ounces",
        "order": "day.desc",
    })
    today_row = next((row for row in week_nutrition if row["day"] == today), None)

    weights = list_weights(config, user_id, 90)
    shots = list_shots(config, user_id, 60)

    effects_today = []
    for log in list_side_effects(config, user_id, 1):
        if log["log_date"] == today:
            effects_today = log["effects"]

    sleep_checkins: list[dict] = []
    checkins = _select(config, "checkins", {
        "user_id": f"eq.{user_id}", "deleted_at": "is.null",
        "checkin_date": f"gte.{week_ago}",
        "select": "id,checkin_date", "order": "checkin_date.desc",
    })
    if checkins:
        ids = ",".join(row["id"] for row in checkins)
        answers = _select(config, "checkin_answers", {
            "checkin_id": f"in.({ids})", "question_id": "eq.sleep",
            "select": "checkin_id,option_code",
        })
        options = _select(config, "checkin_options", {
            "question_id": "eq.sleep", "select": "code,label,value",
        })
        by_code = {opt["code"]: opt for opt in options}
        by_checkin = {a["checkin_id"]: a["option_code"] for a in answers}
        for row in checkins:
            code = by_checkin.get(row["id"])
            option = by_code.get(code or "")
            if option:
                sleep_checkins.append({
                    "checkin_date": row["checkin_date"],
                    "value": option["value"],
                    "label": option["label"],
                })

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
