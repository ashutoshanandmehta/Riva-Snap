"""Persistence for scheduled_meets, through the `0007_scheduled_meets.sql`
SECURITY DEFINER functions — the same server-authoritative pattern as
`chat/store.py`. Nothing here writes to `scheduled_meets` directly; every
write goes through `create_scheduled_meet`, which stamps the verified user id.
"""

import logging

from .. import backend
from ..config import Settings

logger = logging.getLogger("scan.meets.store")

# Matches the `profiles.timezone` column default used across the backend.
_DEFAULT_TIMEZONE = "America/New_York"

_PROFILE_COLUMNS = "name,timezone"


def create_meet(config: Settings, user_id: str, row: dict) -> dict:
    """Inserts one confirmed booking; returns the stored row."""
    rows = backend._rpc(
        config,
        "create_scheduled_meet",
        {
            "p_user_id": user_id,
            "p_person_role": row["person_role"],
            "p_person_name": row["person_name"],
            "p_reason_category": row["reason_category"],
            "p_reason_note": row.get("reason_note"),
            "p_starts_at": row["starts_at"],
            "p_ends_at": row["ends_at"],
            "p_cal_booking_uid": row["cal_booking_uid"],
            "p_meeting_url": row.get("meeting_url"),
            "p_is_mock": row.get("is_mock", False),
        },
    )
    if not rows:
        raise RuntimeError("create_scheduled_meet returned no row")
    return rows[0]


def list_meets(config: Settings, user_id: str, limit: int) -> list[dict]:
    """The user's upcoming, confirmed bookings, soonest first."""
    return backend._rpc(config, "list_scheduled_meets", {"p_user_id": user_id, "p_limit": limit})


def booking_context(config: Settings, user_id: str) -> dict:
    """The attendee details a Cal.com booking needs: name, email, timezone.

    Name and timezone come from `profiles`; email has no column there (it
    lives only in auth.users/GoTrue), so it is resolved separately via
    `backend.user_email` and may come back None on a lookup failure — the
    caller decides whether that blocks booking.
    """
    rows = backend._select(config, "profiles", {"id": f"eq.{user_id}", "select": _PROFILE_COLUMNS})
    profile = rows[0] if rows else {}

    email = backend.user_email(config, user_id)
    if email is None:
        logger.warning(
            "could not resolve email for user %s; booking context is incomplete", user_id
        )

    return {
        "name": profile.get("name") or "",
        "email": email,
        "timezone": profile.get("timezone") or _DEFAULT_TIMEZONE,
    }
