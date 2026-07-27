"""Reconciles the two daily records that describe how a user felt.

Riva stores "how did today go" in two places, on **opposite 1-to-5 scales**:

- `checkin_answers` -> `checkin_options.value`: 1..5 where **5 is best**
  (`nausea`: severe=1 ... none=5).
- `side_effect_log_items.severity`: 1..5 where **5 is worst**.

Both are unique per user per day, and `nausea` appears in both. Handing raw rows
from both to a model guarantees wrong answers — "your nausea is a 5" is either the
best or the worst possible day depending on which table it came from, and any
trend mixing the two is meaningless.

So this module converts everything onto one canonical outward scale before the
data can reach a prompt: **`severity` 1..5, higher = worse**. That direction was
chosen because 7 of the 9 side effects have no check-in counterpart and are
already stored that way, so only the symptom check-ins need converting.

It is deliberately pure — no config, no user id, no I/O — so the conversion is
cheap to test exhaustively and a future `retrieve_side_effects` or `log_symptom`
tool reuses it instead of re-deriving any of this. Same spirit as `grounding.py`
and `plausibility.py`.
"""

import logging

logger = logging.getLogger("scan.chat.symptoms")

# Canonical outward scale for anything comparable as a symptom burden.
CANONICAL_SCALE = "severity_1_5_higher_worse"
# Every checkin_options row is 1..5 with 5 = best (see the seed comment in
# 0002_logging.sql), so one label covers the whole native check-in set.
NATIVE_CHECKIN_SCALE = "value_1_5_higher_better"
# Wellbeing keeps that native direction; it is not a burden measure.
WELLBEING_SCALE = NATIVE_CHECKIN_SCALE
# Appetite is its own thing — see APPETITE_QUESTIONS below.
APPETITE_SCALE = "value_1_5_higher_more_appetite"

SYMPTOM_CATEGORY = "symptoms"
WELLBEING_CATEGORY = "wellbeing"

SOURCE_SIDE_EFFECT_LOG = "side_effect_log"
SOURCE_CHECKIN = "checkin"

# Symptom check-in questions that measure a burden, and so are directly
# comparable with a side_effect_log_items severity once converted.
BURDEN_QUESTIONS = frozenset({"nausea"})

# Symptom-category questions that are NOT a burden. "Strong appetite" is not a
# symptom, and on a GLP-1 low appetite is the intended effect rather than a
# complaint — the direction is genuinely ambiguous. Converting these into a
# "severity" would commit exactly the category error this module exists to
# prevent, so they are reported on their own labelled scale and excluded from
# every symptom total.
APPETITE_QUESTIONS = frozenset({"appetite"})

_VALID = range(1, 6)


def to_severity(value: object) -> int | None:
    """`checkin_options.value` (5 = best) -> canonical severity (5 = worst).

    Returns None for anything outside 1..5 rather than emitting a nonsense
    severity. The DB CHECK constrains the column, so this only fires if that
    guarantee is ever lost.
    """
    try:
        numeric = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning("check-in value %r is not a number; dropping it", value)
        return None
    if numeric not in _VALID:
        logger.warning("check-in value %r is outside 1..5; dropping it", value)
        return None
    return 6 - numeric


def _valid_severity(value: object) -> int | None:
    """A side-effect severity is already canonical; just sanity-check it."""
    try:
        numeric = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning("side-effect severity %r is not a number; dropping it", value)
        return None
    if numeric not in _VALID:
        logger.warning("side-effect severity %r is outside 1..5; dropping it", value)
        return None
    return numeric


def _series_summary(points: list[dict], key: str) -> dict:
    """First/latest/change over a date-ordered series — the deterministic answer
    to "is this getting better or worse?", computed here rather than by the model."""
    values = [point[key] for point in points if point.get(key) is not None]
    if not values:
        return {"days": 0, "first": None, "latest": None, "change": None, "max": None}
    return {
        "days": len(values),
        "first": values[0],
        "latest": values[-1],
        "change": values[-1] - values[0],
        "max": max(values),
        "mean": round(sum(values) / len(values), 1),
    }


def build_symptom_timeline(side_effect_logs: list[dict], checkin_answers: list[dict]) -> dict:
    """One merged, normalised symptom timeline, oldest day first.

    `side_effect_logs` are `backend.list_side_effects` rows
    (`log_date`, `note`, `effects[{effect, severity}]`); `checkin_answers` are
    `store.checkin_answers` rows (`checkin_date`, `question_id`, `category`,
    `option_code`, `label`, `value`).

    A day carrying the same symptom in both sources yields **one** entry: the
    side-effect log wins, because it is the explicit symptom tracker and carries
    the day's note. The check-in's converted value rides along as
    `corroborating_severity` so a disagreement stays visible instead of being
    averaged away.
    """
    days: dict[str, dict] = {}

    for log in side_effect_logs:
        day = log.get("log_date")
        if not day:
            continue
        entry = days.setdefault(day, {"date": day, "note": None, "effects": {}})
        entry["note"] = log.get("note")
        for item in log.get("effects") or []:
            severity = _valid_severity(item.get("severity"))
            if severity is None:
                continue
            effect = item.get("effect")
            entry["effects"][effect] = {
                "effect": effect,
                "severity": severity,
                "source": SOURCE_SIDE_EFFECT_LOG,
                "sources": [SOURCE_SIDE_EFFECT_LOG],
                "corroborating_severity": None,
                "corroborating_label": None,
                "agrees": None,
            }

    appetite: list[dict] = []

    for answer in checkin_answers:
        if answer.get("category") != SYMPTOM_CATEGORY:
            continue
        day = answer.get("checkin_date")
        question = answer.get("question_id")
        if not day or not question:
            continue

        if question in APPETITE_QUESTIONS:
            severity_free = _valid_severity(answer.get("value"))
            if severity_free is not None:
                appetite.append(
                    {
                        "date": day,
                        "question_id": question,
                        "value": severity_free,
                        "label": answer.get("label"),
                        "scale": APPETITE_SCALE,
                        "directional": False,
                    }
                )
            continue

        severity = to_severity(answer.get("value"))
        if severity is None:
            continue
        entry = days.setdefault(day, {"date": day, "note": None, "effects": {}})
        existing = entry["effects"].get(question)
        if existing is None:
            entry["effects"][question] = {
                "effect": question,
                "severity": severity,
                "source": SOURCE_CHECKIN,
                "sources": [SOURCE_CHECKIN],
                "corroborating_severity": None,
                "corroborating_label": None,
                "agrees": None,
            }
            continue

        # Both sources covered this symptom today. Keep the side-effect log's
        # number and record the check-in alongside it.
        existing["sources"] = [SOURCE_SIDE_EFFECT_LOG, SOURCE_CHECKIN]
        existing["corroborating_severity"] = severity
        existing["corroborating_label"] = answer.get("label")
        existing["agrees"] = existing["severity"] == severity

    ordered = [
        {
            "date": day["date"],
            "note": day["note"],
            "effects": sorted(day["effects"].values(), key=lambda item: item["effect"]),
        }
        for day in sorted(days.values(), key=lambda day: day["date"])
    ]

    # Per-effect trend across the window, so "is my nausea improving?" is
    # answered by arithmetic rather than by the model eyeballing a list.
    per_effect: dict[str, list[dict]] = {}
    for day in ordered:
        for item in day["effects"]:
            per_effect.setdefault(item["effect"], []).append(
                {"date": day["date"], "severity": item["severity"]}
            )

    return {
        "scale": CANONICAL_SCALE,
        "scale_note": (
            "severity is 1 (mildest) to 5 (worst). Check-in answers were converted"
            " from their native 1-to-5 higher-is-better scale, so the direction here"
            " is uniform: a bigger number is always a worse day."
        ),
        "days": ordered,
        "summary": {
            "days_logged": len(ordered),
            "effects": {
                effect: _series_summary(points, "severity")
                for effect, points in sorted(per_effect.items())
            },
        },
        # Reported separately and never folded into the totals above.
        "non_directional": sorted(
            appetite, key=lambda point: (point["date"], point["question_id"])
        ),
    }


def build_wellbeing_timeline(checkin_answers: list[dict]) -> dict:
    """Mood, energy, and sleep on their **native** higher-is-better scale.

    Kept out of the symptom timeline deliberately: these are not clinical symptom
    data, and blending them into a symptom burden would be wrong.
    """
    series: dict[str, list[dict]] = {}
    for answer in checkin_answers:
        if answer.get("category") != WELLBEING_CATEGORY:
            continue
        day = answer.get("checkin_date")
        question = answer.get("question_id")
        value = _valid_severity(answer.get("value"))
        if not day or not question or value is None:
            continue
        series.setdefault(question, []).append(
            {"date": day, "value": value, "label": answer.get("label")}
        )

    for points in series.values():
        points.sort(key=lambda point: point["date"])

    return {
        "scale": WELLBEING_SCALE,
        "scale_note": (
            "value is 1 (worst) to 5 (best) — the opposite direction to the symptom"
            " severities. Do not compare these numbers with symptom severities."
        ),
        "series": dict(sorted(series.items())),
        "summary": {
            question: _series_summary(points, "value")
            for question, points in sorted(series.items())
        },
    }
