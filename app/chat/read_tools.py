"""Read tool declarations: every tool that only looks at the user's own data.

Descriptions carry real weight here — they are the only thing the model reads
before choosing. Each one states what the tool covers, what the numbers mean,
and where the arithmetic already happened, because a model that recomputes a
trend from raw rows is the failure mode this design exists to prevent.
"""

from . import handlers, handlers_reads
from .spec import ToolSpec

SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="retrieve_weight_log",
        description=(
            "Read the user's own body-weight history, oldest first, with a"
            " server-computed trend summary (first, latest, and net change in"
            " pounds, plus their start and goal weight). Use this for any"
            " question about weight, weight loss, or progress toward a goal."
            " Dates are inclusive ISO YYYY-MM-DD; omit them for recent history."
            " Do not do the arithmetic yourself — the summary is authoritative."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "start_date": {
                    "type": "string",
                    "format": "date",
                    "description": "Inclusive ISO date YYYY-MM-DD.",
                },
                "end_date": {
                    "type": "string",
                    "format": "date",
                    "description": "Inclusive ISO date YYYY-MM-DD.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": handlers.MAX_LIMIT,
                    "description": f"Most recent entries to read (max {handlers.MAX_LIMIT}).",
                },
            },
            "required": [],
        },
        handler=handlers.retrieve_weight_log,
        aliases=("retrieve-weight-log",),
    ),
    ToolSpec(
        name="checkin_questions",
        description=(
            "Return today's daily check-in question set with the user's answers"
            " already filled in, plus which questions are still unanswered. The"
            " questions are fixed configuration (mood, energy, sleep, nausea,"
            " appetite), not generated. Values are 1 to 5 where 5 is BEST for every"
            " question, including nausea, where 'none' is a 5."
            " Note the 'symptoms' questions (nausea, appetite) overlap the"
            " side-effect log, so a nausea answer may already be recorded there —"
            " check retrieve_medical_log before asking the user to report it again."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "category": {
                    "type": "string",
                    "enum": list(handlers.CHECKIN_CATEGORIES),
                    "description": "Optional filter; omit for the full set.",
                },
            },
            "required": [],
        },
        handler=handlers.checkin_questions,
        aliases=("checkin-questions", "checkin"),
    ),
    ToolSpec(
        name="retrieve_medical_log",
        description=(
            "Read the user's own medication and symptom history. Sections:"
            " 'shots' (dose, date, injection site, comfort), 'plan' (the active"
            " medication plan), 'symptoms' (a merged daily symptom timeline built"
            " from the side-effect log AND the symptom check-ins, normalised so"
            " severity is always 1 to 5 with HIGHER MEANING WORSE), and 'wellbeing'"
            " (mood, energy, sleep, on the opposite 1-to-5 scale where higher is"
            " better). Never compare a severity with a wellbeing value."
            " Riva stores NO clinician, provider, or visit notes — the only free text"
            " is the user's own daily side-effect note, so do not claim to know what"
            " a doctor said. Dates are inclusive ISO YYYY-MM-DD; omitting both reads"
            f" the last {handlers.DEFAULT_WINDOW_DAYS} days."
            " Trend numbers in each summary are authoritative; do not recompute them."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "start_date": {
                    "type": "string",
                    "format": "date",
                    "description": "Inclusive ISO date YYYY-MM-DD.",
                },
                "end_date": {
                    "type": "string",
                    "format": "date",
                    "description": "Inclusive ISO date YYYY-MM-DD.",
                },
                "sections": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(handlers.MEDICAL_SECTIONS)},
                    "description": "Optional subset; omit for all four.",
                },
            },
            "required": [],
        },
        handler=handlers.retrieve_medical_log,
        aliases=("retrieve-medical-log",),
    ),
    ToolSpec(
        name="retrieve_nutrition_log",
        description=(
            "Read the user's own food and hydration history: one row per day with"
            " calories, protein, carbs, fibre and water ounces, their daily goals,"
            " and a server-computed summary (daily averages, best and worst day,"
            " how many days hit each goal). Optionally include the individual"
            " logged meals for the window. Use this for anything about eating,"
            " calories, protein, hydration, or logging streaks."
            " Water is in fluid ounces; only plain water counts toward it."
            " Days with no scan are absent, not zero — the summary's"
            " 'days_logged' says how many days actually have data."
            " Dates are inclusive ISO YYYY-MM-DD; omitting both reads the last"
            f" {handlers_reads.DEFAULT_WINDOW_DAYS} days."
            " Averages in the summary are authoritative; do not recompute them."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "start_date": {
                    "type": "string",
                    "format": "date",
                    "description": "Inclusive ISO date YYYY-MM-DD.",
                },
                "end_date": {
                    "type": "string",
                    "format": "date",
                    "description": "Inclusive ISO date YYYY-MM-DD.",
                },
                "include_meals": {
                    "type": "boolean",
                    "description": (
                        "Add the individual logged meals with their item names."
                        " Omit unless the user asks what they ate."
                    ),
                },
            },
            "required": [],
        },
        handler=handlers_reads.retrieve_nutrition_log,
        aliases=("retrieve-nutrition-log", "nutrition"),
    ),
    ToolSpec(
        name="retrieve_wellness_log",
        description=(
            "Read the user's own wellness practice history — breathing, movement"
            " and mindfulness sessions with their minutes — plus today's total"
            " minutes and current streak, and the daily minutes goal. Use this for"
            " questions about practice, streaks, consistency, or 'have I done my"
            " session today'. Minutes are whole minutes; a streak is consecutive"
            " days with at least one session, computed by the server."
            " Dates are inclusive ISO YYYY-MM-DD; omitting both reads the last"
            f" {handlers_reads.DEFAULT_WINDOW_DAYS} days."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "start_date": {
                    "type": "string",
                    "format": "date",
                    "description": "Inclusive ISO date YYYY-MM-DD.",
                },
                "end_date": {
                    "type": "string",
                    "format": "date",
                    "description": "Inclusive ISO date YYYY-MM-DD.",
                },
            },
            "required": [],
        },
        handler=handlers_reads.retrieve_wellness_log,
        aliases=("retrieve-wellness-log", "wellness"),
    ),
    ToolSpec(
        name="retrieve_profile_goals",
        description=(
            "Read what the user is aiming for: start and goal weight, height,"
            " timezone, their daily nutrition and wellness targets, which health"
            " goals they selected at signup (GLP-1 support, weight management,"
            " nutrition, muscle, movement, sleep), and their active medication"
            " plan. Use this when an answer needs a target to compare against, or"
            " when the user asks what their goals are. This returns no identifying"
            " details — no name, date of birth, or clinician."
            " It is settings, not history: for actual progress call"
            " retrieve_weight_log or retrieve_nutrition_log."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "required": [],
        },
        handler=handlers_reads.retrieve_profile_goals,
        aliases=("retrieve-profile-goals", "goals"),
    ),
    ToolSpec(
        name="retrieve_todos",
        description=(
            "Read the user's open to-dos — title, category (food, water, weight,"
            " custom), whether it repeats daily or fires once, its reminder time,"
            " and whether it is already done for today. Done state is resolved"
            " against the user's own calendar day, so a daily to-do resets each"
            " morning. Call this before changing or completing a to-do: the"
            " `todo_id` those tools need comes from here, and must never be"
            " invented."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "required": [],
        },
        handler=handlers_reads.retrieve_todos,
        aliases=("retrieve-todos", "todos"),
    ),
)
