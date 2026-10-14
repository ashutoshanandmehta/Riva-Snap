"""`GET /v1/meets/slots`, `POST /v1/meets`, `GET /v1/meets` — Schedule a call.

Entirely server-orchestrated: no client-side Cal.com embed. The route layer
validates, resolves the person and the caller's booking context, then calls
`cal.py` (mock or real, decided by `CAL_COM_API_KEY`) and persists through
`store.py`. See `cal.py`'s module docstring for the fail-soft/fail-hard split.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException, Query

from ..config import settings
from . import cal, store
from .schemas import (
    MEET_REASONS,
    BookMeetRequest,
    MeetListResult,
    MeetSlotDay,
    MeetSlotsResponse,
    ScheduledMeet,
)

logger = logging.getLogger("scan.meets")

router = APIRouter()

MIN_SLOT_DAYS = 1
MAX_SLOT_DAYS = 30
LIST_LIMIT = 20

_SLOTS_UNAVAILABLE = "Times aren't available right now. Try again shortly."
_BOOKING_FAILED = "Couldn't book that time. Pick another."
_NO_EMAIL = "Add an email to your account before booking a call."
_BAD_EMAIL = "Enter a valid email address."
_DEVICE_EMAIL = "Add a real email to book a call."
# Device accounts are provisioned with an address on this domain; it can't
# receive mail, so Cal.com rejects the booking outright.
_DEVICE_EMAIL_DOMAIN = "@devices.riva.app"
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_RECONCILE_NOTE = (
    "Your call may already be booked — we couldn't confirm it on our side. "
    "Check your email before booking another time."
)


def _current_user(authorization: str | None) -> str:
    """Reuses `main._require_user` — the one auth implementation.

    Imported inside the function because `main` imports this router at module
    scope; a top-level import would be circular. Duplicating the auth gate here
    to avoid that would be far worse than a deferred import.
    """
    from ..main import _require_user

    return _require_user(authorization)


def _person_or_400(catalogue: dict, role: str) -> dict:
    person = catalogue.get(role)
    if person is None:
        raise HTTPException(status_code=400, detail=f"Unknown role: {role}")
    return person


def _resolve_timezone(requested: str | None, profile_timezone: str) -> str:
    """The IANA zone to use: an explicit request wins, then the profile's."""
    if requested is None:
        return profile_timezone
    try:
        ZoneInfo(requested)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Unknown timezone: {requested}") from None
    return requested


def _attendee_email(requested: str | None, account_email: str | None) -> str:
    """The address the invite goes to: the typed one, else the account's.

    A malformed typed address is a 400; the synthetic device-account address
    (typed or from the account) is a 400 with its own message rather than a
    Cal.com failure the caller can't act on.
    """
    email = (requested or "").strip().lower() or (account_email or "")
    if requested is not None and requested.strip() and not _EMAIL_PATTERN.match(email):
        raise HTTPException(status_code=400, detail=_BAD_EMAIL)
    if not email:
        raise HTTPException(status_code=400, detail=_NO_EMAIL)
    if email.endswith(_DEVICE_EMAIL_DOMAIN):
        raise HTTPException(status_code=400, detail=_DEVICE_EMAIL)
    return email


def _role_label(catalogue: dict, person_role: str) -> str:
    person = catalogue.get(person_role)
    if person:
        return person.get("role_label", person_role)
    return person_role.replace("_", " ").title()


def _to_scheduled_meet(catalogue: dict, row: dict) -> ScheduledMeet:
    return ScheduledMeet(
        id=row["id"],
        role=row["person_role"],
        person_name=row["person_name"],
        role_label=_role_label(catalogue, row["person_role"]),
        reason_category=row["reason_category"],
        reason_note=row.get("reason_note"),
        starts_at=row["starts_at"],
        ends_at=row["ends_at"],
        meeting_url=row.get("meeting_url"),
        status=row["status"],
        is_mock=row["is_mock"],
    )


@router.get("/v1/meets/slots", response_model=MeetSlotsResponse)
def meet_slots(
    role: str,
    days: int | None = None,
    tz: str | None = Query(default=None, alias="timezone"),
    authorization: str | None = Header(default=None),
) -> MeetSlotsResponse:
    """The next days with open slots for one bookable person, in `timezone`
    (default: the profile's). `days` is how far ahead Cal.com is searched; at
    most `cal_com_slot_days` days that actually have availability come back."""
    user_id = _current_user(authorization)
    config = settings()

    catalogue = cal.person_catalogue(config)
    person = _person_or_400(catalogue, role)
    window = days if days is not None else config.cal_com_slot_lookahead_days
    if not MIN_SLOT_DAYS <= window <= MAX_SLOT_DAYS:
        raise HTTPException(
            status_code=400, detail=f"days must be between {MIN_SLOT_DAYS} and {MAX_SLOT_DAYS}."
        )

    context = store.booking_context(config, user_id)
    tz_name = _resolve_timezone(tz, context["timezone"])
    try:
        local_today = datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        logger.warning("unknown booking timezone %r; using the server day", tz_name)
        local_today = datetime.now(timezone.utc).date()
    end_date = local_today + timedelta(days=window - 1)

    try:
        raw_days = cal.available_slots(config, person, local_today, end_date, tz_name)
    except cal.CalComUnavailable:
        logger.warning("Cal.com slots unavailable for role=%s", role)
        raise HTTPException(status_code=503, detail=_SLOTS_UNAVAILABLE) from None

    open_days = [day for day in sorted(raw_days.keys()) if raw_days[day]]
    days_out = [
        MeetSlotDay(date=day, slots=raw_days[day]) for day in open_days[: config.cal_com_slot_days]
    ]

    return MeetSlotsResponse(
        role=role,
        display_name=person["display_name"],
        role_label=person["role_label"],
        duration_minutes=person["duration_minutes"],
        timezone=tz_name,
        mocked=cal.is_mocked(config),
        days=days_out,
    )


@router.post("/v1/meets", response_model=ScheduledMeet)
def book_meet(
    request: BookMeetRequest, authorization: str | None = Header(default=None)
) -> ScheduledMeet:
    """Books one call and persists it. Cal.com is the source of truth for the
    booking itself; the database write happens only after it confirms."""
    user_id = _current_user(authorization)
    config = settings()

    catalogue = cal.person_catalogue(config)
    person = _person_or_400(catalogue, request.role)

    if request.reason_category not in MEET_REASONS:
        raise HTTPException(status_code=400, detail=f"Unknown reason: {request.reason_category}")
    if request.reason_note is not None and len(request.reason_note) > 500:
        raise HTTPException(status_code=400, detail="Keep the note under 500 characters.")

    try:
        start_dt = datetime.fromisoformat(request.start)
    except ValueError:
        raise HTTPException(status_code=400, detail="start must be an ISO8601 datetime.") from None
    if start_dt.tzinfo is None:
        raise HTTPException(
            status_code=400, detail="start must include a UTC offset (an aware datetime)."
        )
    if start_dt <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="start must be in the future.")

    context = store.booking_context(config, user_id)
    attendee_email = _attendee_email(request.email, context["email"])

    attendee = {
        "name": context["name"] or "Riva member",
        "email": attendee_email,
        "timezone": _resolve_timezone(request.timezone, context["timezone"]),
    }

    try:
        booking = cal.create_booking(
            config,
            person,
            start_iso=request.start,
            attendee=attendee,
            user_id=user_id,
            cc_email=config.cal_com_cc_email or None,
        )
    except cal.CalComUnavailable:
        logger.warning("Cal.com unavailable while booking role=%s for %s", request.role, user_id)
        raise HTTPException(status_code=503, detail=_BOOKING_FAILED) from None
    except cal.CalComBookingFailed:
        logger.warning("Cal.com booking failed for role=%s user=%s", request.role, user_id)
        raise HTTPException(status_code=502, detail=_BOOKING_FAILED) from None

    row = {
        "person_role": request.role,
        "person_name": person["display_name"],
        "reason_category": request.reason_category,
        "reason_note": request.reason_note,
        "starts_at": booking["start"],
        "ends_at": booking["end"],
        "cal_booking_uid": booking["uid"],
        "meeting_url": booking.get("meeting_url"),
        "is_mock": booking["mocked"],
    }

    try:
        stored = store.create_meet(config, user_id, row)
    except Exception:
        # Cal.com has already confirmed this booking — logged loudly (not
        # silently) so it can be reconciled by hand, and the caller is told
        # the booking may exist rather than invited to just try again.
        logger.error(
            "scheduled_meets write failed after a confirmed Cal.com booking: "
            "cal_booking_uid=%s user_id=%s starts_at=%s",
            booking["uid"],
            user_id,
            booking["start"],
        )
        raise HTTPException(status_code=502, detail=_RECONCILE_NOTE) from None

    return _to_scheduled_meet(catalogue, stored)


@router.get("/v1/meets", response_model=MeetListResult)
def list_meets(authorization: str | None = Header(default=None)) -> MeetListResult:
    """The user's upcoming, confirmed calls. An empty list is a valid 200."""
    user_id = _current_user(authorization)
    config = settings()
    catalogue = cal.person_catalogue(config)
    rows = store.list_meets(config, user_id, LIST_LIMIT)
    return MeetListResult(meets=[_to_scheduled_meet(catalogue, row) for row in rows])
