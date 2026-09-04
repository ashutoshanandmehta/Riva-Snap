"""Cal.com client: person catalogue, availability, and booking creation.

Mock mode (`CAL_COM_API_KEY` empty) is fail-soft everywhere — malformed
config degrades to a placeholder catalogue rather than raising. Real mode is
the opposite: a Cal.com failure with a key configured must never fall back to
mock data, so it raises one of the exceptions below and the route turns that
into a clear 502/503 instead of a silently wrong answer.
"""

import json
import logging
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from ..config import Settings

logger = logging.getLogger("scan.meets.cal")

# Pooled client: person lookups, slot reads, and booking writes all share one
# connection pool rather than opening a socket per call.
_http = httpx.Client(timeout=8.0)

# Cal.com versions its API per endpoint via this header; the slots and
# bookings endpoints are pinned to different versions, so each gets its own
# named constant rather than a shared one that would silently drift.
SLOTS_API_VERSION = "2024-09-04"
BOOKINGS_API_VERSION = "2026-02-25"

# No real Cal.com event types configured yet. Used whenever
# `cal_com_event_types` is empty or fails to parse as the documented JSON
# shape — a broken/missing config must degrade the feature, not break it.
_PLACEHOLDER_CATALOGUE = {
    "clinician": {
        "display_name": "Dr. Amara Chen",
        "role_label": "Clinician",
        "cal_username": "peptide-clinician",
        "event_type_slug": "clinician-call",
        "duration_minutes": 30,
    },
    "wellness_coach": {
        "display_name": "Maya Ellis",
        "role_label": "Wellness coach",
        "cal_username": "peptide-coach",
        "event_type_slug": "coach-call",
        "duration_minutes": 30,
    },
}

# Mock-mode availability window: weekday business hours only.
_MOCK_SLOT_START_HOUR = 9
_MOCK_SLOT_END_HOUR = 16
_MOCK_SLOT_MINUTES = 30


class CalComUnavailable(Exception):
    """Cal.com could not be reached, or answered with a non-2xx status."""


class CalComBookingFailed(Exception):
    """Cal.com answered the booking request but not with a usable booking."""


def person_catalogue(config: Settings) -> dict[str, dict]:
    """The bookable people, keyed by role. Falls back to a placeholder
    catalogue on an empty or malformed `cal_com_event_types`."""
    raw = config.cal_com_event_types
    if not raw:
        logger.warning("CAL_COM_EVENT_TYPES is empty; using the placeholder catalogue")
        return _PLACEHOLDER_CATALOGUE
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("CAL_COM_EVENT_TYPES must decode to a JSON object")
        return parsed
    except (ValueError, TypeError) as error:
        logger.warning(
            "CAL_COM_EVENT_TYPES is malformed (%s); using the placeholder catalogue", error
        )
        return _PLACEHOLDER_CATALOGUE


def is_mocked(config: Settings) -> bool:
    return not config.cal_com_api_key


def _mock_day_slots(day: date, duration_minutes: int, now: datetime, tz: ZoneInfo) -> list[str]:
    if day.weekday() >= 5:  # Saturday/Sunday
        return []
    slots: list[str] = []
    cursor = datetime.combine(day, datetime.min.time(), tzinfo=tz).replace(
        hour=_MOCK_SLOT_START_HOUR
    )
    end_of_window = cursor.replace(hour=_MOCK_SLOT_END_HOUR)
    step = timedelta(minutes=_MOCK_SLOT_MINUTES)
    while cursor + timedelta(minutes=duration_minutes) <= end_of_window:
        if cursor > now:
            slots.append(cursor.isoformat())
        cursor += step
    return slots


def _mock_available_slots(
    person: dict, start_date: date, end_date: date, timezone: str
) -> dict[str, list[str]]:
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        logger.warning("unknown booking timezone %r; falling back to UTC", timezone)
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    duration_minutes = person.get("duration_minutes", 30)

    days: dict[str, list[str]] = {}
    cursor_date = start_date
    while cursor_date <= end_date:
        days[cursor_date.isoformat()] = _mock_day_slots(cursor_date, duration_minutes, now, tz)
        cursor_date += timedelta(days=1)
    return days


def _real_available_slots(
    config: Settings, person: dict, start_date: date, end_date: date, timezone: str
) -> dict[str, list[str]]:
    try:
        response = _http.get(
            f"{config.cal_com_base_url}/slots",
            headers={
                "Authorization": f"Bearer {config.cal_com_api_key}",
                "cal-api-version": SLOTS_API_VERSION,
            },
            params={
                "eventTypeSlug": person.get("event_type_slug"),
                "username": person.get("cal_username"),
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
                "timeZone": timezone,
            },
            timeout=config.cal_com_timeout_s,
        )
    except httpx.HTTPError as error:
        logger.error("Cal.com slots request failed: %s", error)
        raise CalComUnavailable(str(error)) from error

    if not 200 <= response.status_code < 300:
        logger.warning(
            "Cal.com slots call failed: %s %s", response.status_code, response.text[:300]
        )
        raise CalComUnavailable(f"Cal.com returned {response.status_code}")

    try:
        body = response.json()
    except ValueError as error:
        logger.error("Cal.com slots response was not JSON: %s", error)
        raise CalComUnavailable("Cal.com returned an unreadable response") from error

    # Defensive: never assume Cal.com's exact shape. The live v2 slots
    # response nests day -> list of {"start": iso} entries under "data"
    # (confirmed against the real API — earlier docs/examples say "time",
    # so both are checked rather than trusting either alone).
    raw_days = body.get("data") if isinstance(body, dict) else None
    days: dict[str, list[str]] = {}
    if isinstance(raw_days, dict):
        for day_key, entries in raw_days.items():
            times: list[str] = []
            for entry in entries or []:
                if isinstance(entry, dict):
                    value = entry.get("start") or entry.get("time")
                    if value:
                        times.append(value)
                elif isinstance(entry, str):
                    times.append(entry)
            days[day_key] = times
    return days


def available_slots(
    config: Settings, person: dict, start_date: date, end_date: date, timezone: str
) -> dict[str, list[str]]:
    """Date -> offset-aware ISO8601 slot starts, in mock or real mode."""
    if is_mocked(config):
        return _mock_available_slots(person, start_date, end_date, timezone)
    return _real_available_slots(config, person, start_date, end_date, timezone)


def _mock_create_booking(person: dict, start_iso: str) -> dict:
    duration_minutes = person.get("duration_minutes", 30)
    start_dt = datetime.fromisoformat(start_iso)
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    booking_uid = f"mock-{uuid.uuid4().hex[:12]}"
    return {
        "uid": booking_uid,
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "meeting_url": f"https://cal.com/mock-meeting/{booking_uid}",
        "mocked": True,
    }


def _extract_meeting_url(data: dict) -> str | None:
    """Best-effort meeting link lookup. Cal.com's own response shape for this
    is not something a broken/missing key should ever 500 on."""
    for key in ("meetingUrl", "meeting_url"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    location = data.get("location")
    if isinstance(location, dict):
        for key in ("link", "meetingUrl", "optionValue"):
            value = location.get(key)
            if isinstance(value, str) and value:
                return value
    references = data.get("references")
    if isinstance(references, list):
        for reference in references:
            if isinstance(reference, dict) and isinstance(reference.get("meetingUrl"), str):
                return reference["meetingUrl"]
    return None


def _real_create_booking(
    config: Settings,
    person: dict,
    start_iso: str,
    attendee: dict,
    user_id: str,
    cc_email: str | None,
) -> dict:
    body = {
        "eventTypeSlug": person.get("event_type_slug"),
        "username": person.get("cal_username"),
        "start": start_iso,
        "attendee": {
            "name": attendee.get("name"),
            "email": attendee.get("email"),
            "timeZone": attendee.get("timezone"),
        },
        "location": {"type": "integration", "integration": "google-meet"},
        "metadata": {"patientId": user_id},
    }
    if cc_email:
        body["guests"] = [cc_email]

    try:
        response = _http.post(
            f"{config.cal_com_base_url}/bookings",
            headers={
                "Authorization": f"Bearer {config.cal_com_api_key}",
                "cal-api-version": BOOKINGS_API_VERSION,
            },
            json=body,
            timeout=config.cal_com_booking_timeout_s,
        )
    except httpx.HTTPError as error:
        logger.error("Cal.com booking request failed: %s", error)
        raise CalComUnavailable(str(error)) from error

    if not 200 <= response.status_code < 300:
        logger.error(
            "Cal.com booking call failed: %s %s", response.status_code, response.text[:300]
        )
        raise CalComBookingFailed(f"Cal.com returned {response.status_code}")

    try:
        payload = response.json()
    except ValueError as error:
        logger.error("Cal.com booking response was not JSON: %s", error)
        raise CalComBookingFailed("Cal.com returned an unreadable response") from error

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else {}

    booking_uid = data.get("uid")
    if not booking_uid:
        logger.error("Cal.com booking response had no uid: %s", str(data)[:300])
        raise CalComBookingFailed("Cal.com did not confirm a booking")

    duration_minutes = person.get("duration_minutes", 30)
    end = data.get("end")
    if not end:
        end = (datetime.fromisoformat(start_iso) + timedelta(minutes=duration_minutes)).isoformat()

    return {
        "uid": booking_uid,
        "start": data.get("start") or start_iso,
        "end": end,
        "meeting_url": _extract_meeting_url(data),
        "mocked": False,
    }


def create_booking(
    config: Settings,
    person: dict,
    start_iso: str,
    attendee: dict,
    user_id: str,
    cc_email: str | None = None,
) -> dict:
    """Books the slot; returns `{uid, start, end, meeting_url, mocked}`.

    Mock mode synthesises the booking. Real mode calls Cal.com and raises
    `CalComBookingFailed` rather than ever substituting mock data — a real key
    configured means a Cal.com failure is a failure, not a silent fallback.
    """
    if is_mocked(config):
        return _mock_create_booking(person, start_iso)
    return _real_create_booking(config, person, start_iso, attendee, user_id, cc_email)
