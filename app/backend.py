"""Supabase integration: token verification and server-authoritative writes.

The client only ever authenticates (email OTP via supabase-js). All database
writes go through this module with the service role key, calling the
log_scan() Postgres function, which stamps the verified user id and updates
food_entries plus the nutrition_days daily aggregate in one transaction.
"""

import hashlib
import hmac
import logging

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
