"""Supabase integration: token verification and server-authoritative writes.

The client only ever authenticates (email OTP via supabase-js). All database
writes go through this module with the service role key, calling the
log_scan() Postgres function, which stamps the verified user id and updates
food_entries plus the nutrition_days daily aggregate in one transaction.
"""

import logging

import httpx
from fastapi import HTTPException

from .config import Settings

logger = logging.getLogger("scan.backend")

_http = httpx.Client(timeout=8.0)


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


def log_scan(config: Settings, user_id: str, entry: dict) -> dict:
    """Persists an accepted scan via the log_scan RPC; returns day totals."""
    key = config.supabase_service_role_key
    headers = {"apikey": key, "Content-Type": "application/json"}
    if key.startswith("eyJ"):
        # Legacy service_role keys are JWTs and also go in the Authorization
        # header. New sb_secret_ keys must not: they are not JWTs.
        headers["Authorization"] = f"Bearer {key}"
    try:
        response = _http.post(
            f"{config.supabase_url}/rest/v1/rpc/log_scan",
            headers=headers,
            json={
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
    except httpx.HTTPError as error:
        raise HTTPException(status_code=503, detail=f"Backend unreachable: {error}") from error

    if response.status_code != 200:
        logger.error("log_scan RPC failed: %s %s", response.status_code, response.text[:300])
        raise HTTPException(status_code=502, detail="Could not save the log. Try again.")

    rows = response.json()
    if not rows:
        raise HTTPException(status_code=502, detail="Log saved but totals were not returned.")
    return rows[0]
