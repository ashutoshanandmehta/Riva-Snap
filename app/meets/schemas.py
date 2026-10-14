"""Request/response models for `/v1/meets`."""

from pydantic import BaseModel

# Mirrors the `reason_category` CHECK in 0007_scheduled_meets.sql — kept here,
# not derived from the DB, so a bad category is rejected at the route before
# any Cal.com call or database round trip.
MEET_REASONS = ("side_effects", "dose_adjustment", "general_checkin", "other")

MEET_REASON_LABELS = {
    "side_effects": "Side effects",
    "dose_adjustment": "Dose adjustment",
    "general_checkin": "General check-in",
    "other": "Something else",
}


class MeetSlotDay(BaseModel):
    """One calendar day's open slots, each an offset-aware ISO8601 datetime."""

    date: str
    slots: list[str]


class MeetSlotsResponse(BaseModel):
    role: str
    display_name: str
    role_label: str
    duration_minutes: int
    timezone: str
    # True whenever CAL_COM_API_KEY is unset — the app badges these as
    # placeholder times rather than presenting them as real availability.
    mocked: bool
    days: list[MeetSlotDay]


class BookMeetRequest(BaseModel):
    role: str
    # Offset-aware ISO8601 (e.g. "2026-09-01T14:00:00-04:00"); a naive or
    # unparseable value is rejected at the route, not assumed to be UTC.
    start: str
    reason_category: str
    reason_note: str | None = None
    # IANA name the attendee is booking in; falls back to the profile timezone.
    timezone: str | None = None


class ScheduledMeet(BaseModel):
    id: str
    role: str
    person_name: str
    role_label: str
    reason_category: str
    reason_note: str | None = None
    starts_at: str
    ends_at: str
    meeting_url: str | None = None
    status: str
    is_mock: bool


class MeetListResult(BaseModel):
    meets: list[ScheduledMeet] = []
