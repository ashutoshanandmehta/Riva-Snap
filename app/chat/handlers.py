"""Tool handlers: `(config, user_id, arguments) -> JSON-safe dict`.

Each one reads only the calling user's own rows, through the existing
`app/backend.py` helpers. Trend arithmetic happens here, in Python, not in the
model — the same principle the scan pipeline uses with USDA grounding: the LLM
narrates, deterministic code computes.
"""

import logging
from datetime import date, timedelta

from fastapi import HTTPException

from .. import backend
from ..config import Settings
from . import store, symptoms

logger = logging.getLogger("scan.chat.handlers")

MAX_LIMIT = 200
_DEFAULT_LIMIT = 60

# The four things a user actually asks about, deliberately not the table names —
# `symptoms` merges two tables, `wellbeing` splits one out of a third.
MEDICAL_SECTIONS = ("shots", "plan", "symptoms", "wellbeing")
CHECKIN_CATEGORIES = (symptoms.WELLBEING_CATEGORY, symptoms.SYMPTOM_CATEGORY)

# Window used when the caller names no dates. Stated in the tool description so
# the model knows what "recently" resolved to.
DEFAULT_WINDOW_DAYS = 30


def iso_date(value: object, field: str) -> str | None:
    """Validates an ISO date argument. A model can emit "last month" or
    "2026-13-01"; either is a clear 400, never a silent whole-history read.

    Called from two places on purpose, not by duplication: `router.py` runs it
    while parsing a typed command, so a bad date is rejected before anything is
    persisted, and the handler runs it again because the conversational path
    reaches here without ever passing through the parser.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{field} must be a date like 2026-07-01.")
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"{field} must be a date like 2026-07-01."
        ) from None


def _limit(value: object) -> int:
    if value is None or value == "":
        return _DEFAULT_LIMIT
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="limit must be a whole number.") from None
    return max(1, min(parsed, MAX_LIMIT))


def _rounded(value: object, places: int = 1) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), places)
    except (TypeError, ValueError):
        return None


def retrieve_weight_log(config: Settings, user_id: str, arguments: dict) -> dict:
    """Weight history plus a computed trend summary, oldest entry first."""
    start_date = iso_date(arguments.get("start_date"), "start_date")
    end_date = iso_date(arguments.get("end_date"), "end_date")
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date is after end_date.")

    rows = backend.list_weights(
        config, user_id, _limit(arguments.get("limit")), since=start_date, until=end_date
    )
    # The query takes the most recent `limit` rows; reverse so the series reads
    # forward in time, which is how a trend is described.
    rows = list(reversed(rows))

    entries = [
        {
            "measured_at": row.get("measured_at"),
            "pounds": _rounded(row.get("pounds"), 2),
            "dose_mg": _rounded(row.get("dose_mg"), 2),
        }
        for row in rows
    ]

    targets = store.profile_targets(config, user_id)
    first_lbs = entries[0]["pounds"] if entries else None
    latest_lbs = entries[-1]["pounds"] if entries else None
    goal_weight = _rounded(targets.get("goal_weight"), 2)

    summary = {
        "count": len(entries),
        "first_lbs": first_lbs,
        "latest_lbs": latest_lbs,
        "change_lbs": (
            round(latest_lbs - first_lbs, 1)
            if first_lbs is not None and latest_lbs is not None
            else None
        ),
        "period_start": entries[0]["measured_at"] if entries else None,
        "period_end": entries[-1]["measured_at"] if entries else None,
        "start_weight_lbs": _rounded(targets.get("start_weight"), 2),
        "goal_weight_lbs": goal_weight,
        "to_goal_lbs": (
            round(latest_lbs - goal_weight, 1)
            if latest_lbs is not None and goal_weight is not None
            else None
        ),
    }

    logger.info("retrieve_weight_log: %d entries", len(entries))
    return {
        "requested_range": {"start_date": start_date, "end_date": end_date},
        "entries": entries,
        "summary": summary,
    }


def checkin_questions(config: Settings, user_id: str, arguments: dict) -> dict:
    """Today's check-in set with the user's answers already resolved.

    Deterministic — the questions are seeded configuration, not generated. Values
    stay on their **native** 1-to-5 higher-is-better scale here, because that is
    what the client renders and what `log_checkin` accepts. Conversion onto the
    symptom severity scale happens only in `retrieve_medical_log`, where these
    answers have to sit next to side-effect severities.
    """
    category = arguments.get("category")
    if category is not None and category not in CHECKIN_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"category must be one of: {', '.join(CHECKIN_CATEGORIES)}.",
        )

    day, tz_name = store.local_day(config, user_id)
    catalogue = store.checkin_config(config)
    answered = {
        row["question_id"]: row
        for row in store.checkin_answers(config, user_id, since=day, until=day)
    }

    questions: list[dict] = []
    for question in catalogue["questions"]:
        if category and question.get("category") != category:
            continue
        answer = answered.get(question["id"])
        questions.append(
            {
                "id": question["id"],
                "category": question.get("category"),
                "title": question.get("title"),
                "subtitle": question.get("subtitle"),
                "options": [
                    {
                        "code": option["code"],
                        "label": option.get("label"),
                        "value": option.get("value"),
                    }
                    for option in catalogue["options"].get(question["id"], [])
                ],
                "answered_option_code": (answer or {}).get("option_code"),
                "answered_label": (answer or {}).get("label"),
                "answered_value": (answer or {}).get("value"),
            }
        )

    unanswered = [q["id"] for q in questions if q["answered_option_code"] is None]
    logger.info("checkin_questions: %d questions, %d unanswered", len(questions), len(unanswered))
    return {
        "day": day,
        "timezone": tz_name,
        "scale": symptoms.NATIVE_CHECKIN_SCALE,
        "scale_note": (
            "value is 1 (worst) to 5 (best) for every question, including the symptom"
            " ones — 'none' for nausea is a 5. Use option codes when recording an answer."
        ),
        "questions": questions,
        "answered_count": len(questions) - len(unanswered),
        "unanswered": unanswered,
    }


def _validated_sections(value: object) -> tuple[str, ...]:
    if value is None or value == "" or value == []:
        return MEDICAL_SECTIONS
    requested = [value] if isinstance(value, str) else value
    if not isinstance(requested, list):
        raise HTTPException(status_code=400, detail="sections must be a list of names.")
    unknown = sorted({item for item in requested if item not in MEDICAL_SECTIONS})
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown section(s) {', '.join(map(str, unknown))}."
                f" Valid sections: {', '.join(MEDICAL_SECTIONS)}."
            ),
        )
    # Canonical order, deduplicated, so the payload shape never depends on the
    # order the caller happened to list them in.
    chosen = set(requested)
    return tuple(section for section in MEDICAL_SECTIONS if section in chosen)


def retrieve_medical_log(config: Settings, user_id: str, arguments: dict) -> dict:
    """Medication and symptom history, assembled from the tables that exist.

    `symptoms` is the merged, scale-normalised timeline built by
    `chat/symptoms.py` — side-effect logs and symptom check-ins reconciled onto
    one severity scale. `wellbeing` is mood/energy/sleep, kept separate on their
    native higher-is-better scale.
    """
    start_date = iso_date(arguments.get("start_date"), "start_date")
    end_date = iso_date(arguments.get("end_date"), "end_date")
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date is after end_date.")
    sections = _validated_sections(arguments.get("sections"))

    if not start_date and not end_date:
        # Anchor the default window on the user's own day, the way every log_*
        # function does, rather than on the server's date.
        today, _ = store.local_day(config, user_id)
        end_date = today
        start_date = (date.fromisoformat(today) - timedelta(days=DEFAULT_WINDOW_DAYS)).isoformat()

    result: dict = {
        "requested_range": {"start_date": start_date, "end_date": end_date},
        "sections": list(sections),
        # Stated in the payload as well as the tool description: there is no
        # clinician-notes table, so the model must not imply it has one.
        "note_provenance": (
            "Any note here is the user's own daily side-effect note. Riva stores no"
            " clinician, provider, or visit notes."
        ),
    }

    if "shots" in sections:
        rows = backend.list_shots(config, user_id, MAX_LIMIT, since=start_date, until=end_date)
        result["shots"] = {
            "entries": [
                {
                    "taken_at": row.get("taken_at"),
                    "medication_name": row.get("medication_name"),
                    "dose_mg": _rounded(row.get("dose_mg"), 2),
                    "injection_site": row.get("injection_site"),
                    "comfort_rating": row.get("comfort_rating"),
                }
                # Oldest first, matching the weight series, so a dose history reads
                # forward in time.
                for row in reversed(rows)
            ],
            "comfort_scale": "comfort is 1 (worst) to 5 (best).",
        }

    if "plan" in sections:
        result["plan"] = store.active_plan(config, user_id)

    answers: list[dict] = []
    if "symptoms" in sections or "wellbeing" in sections:
        answers = store.checkin_answers(config, user_id, since=start_date, until=end_date)

    if "symptoms" in sections:
        logs = backend.list_side_effects(
            config, user_id, DEFAULT_WINDOW_DAYS, since=start_date, until=end_date
        )
        result["symptoms"] = symptoms.build_symptom_timeline(logs, answers)

    if "wellbeing" in sections:
        result["wellbeing"] = symptoms.build_wellbeing_timeline(answers)

    logger.info("retrieve_medical_log: sections=%s", list(sections))
    return result
