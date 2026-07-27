"""Write tool declarations — the tools that change the user's data.

Every spec here sets `writes=True` and supplies a `confirm_summary`, the exact
sentence the user is shown before anything is saved. `confirm.py` enforces the
two-step; these descriptions tell the model why, so it asks naturally instead of
treating the preview as an error to route around.

Nothing here can be used to write on behalf of anyone else: the subject is bound
from the verified token in `tools.dispatch`, and `ToolSpec` rejects at import
time any schema that tries to name one.
"""

from . import handlers_writes as writes
from .confirm import CONFIRM_KEY, CONFIRM_PROPERTY
from .spec import ToolSpec

_CONFIRM_NOTE = (
    " This tool WRITES to the user's log. Calling it without confirm=true saves"
    " nothing and returns a preview; show that to the user, get a clear yes, then"
    " call again with confirm=true and the same values."
)


def _weight_summary(arguments: dict) -> str:
    when = arguments.get("measured_at") or "today"
    return f"Record a weigh-in of {arguments.get('pounds')} lbs for {when}."


def _side_effects_summary(arguments: dict) -> str:
    effects = arguments.get("effects") or []
    listed = ", ".join(
        f"{item.get('effect')} at {item.get('severity')}/5"
        for item in effects
        if isinstance(item, dict)
    )
    note = arguments.get("note")
    tail = f' with the note "{note}"' if note else ""
    return (
        f"Set today's side effects to {listed or 'nothing'}{tail}."
        " This REPLACES anything already logged for today — effects left off the"
        " list are removed."
    )


def _set_todo_summary(arguments: dict) -> str:
    verb = "Update the to-do" if arguments.get("todo_id") else "Create a to-do"
    when = (
        f"once on {arguments.get('due_date')}"
        if arguments.get("repeat_rule") == "once"
        else "every day"
    )
    hour, minute = arguments.get("remind_hour"), arguments.get("remind_minute")
    time = f"{hour:02d}:{minute:02d}" if isinstance(hour, int) and isinstance(minute, int) else "?"
    return (
        f'{verb} "{arguments.get("title")}" in the'
        f" {arguments.get('category')} category, reminding {when} at {time}."
    )


def _complete_todo_summary(arguments: dict) -> str:
    state = "not done" if arguments.get("done") is False else "done"
    return f"Mark to-do {arguments.get('todo_id')} as {state} for today."


def _remove_todo_summary(arguments: dict) -> str:
    return f"Delete to-do {arguments.get('todo_id')} permanently."


SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="record_weight",
        description=(
            "Record one body-weight measurement in pounds. Use only when the user"
            " states a weight they want logged — never a weight you inferred or"
            " estimated. Omit measured_at for today; otherwise give the inclusive"
            " ISO date the user weighed themselves." + _CONFIRM_NOTE
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "pounds": {
                    "type": "number",
                    "minimum": writes.MIN_POUNDS,
                    "maximum": writes.MAX_POUNDS,
                    "description": "Body weight in pounds, as the user stated it.",
                },
                "measured_at": {
                    "type": "string",
                    "format": "date",
                    "description": "ISO date YYYY-MM-DD. Omit for today.",
                },
                CONFIRM_KEY: CONFIRM_PROPERTY,
            },
            "required": ["pounds"],
        },
        handler=writes.record_weight,
        aliases=("record-weight", "log-weight"),
        writes=True,
        confirm_summary=_weight_summary,
    ),
    ToolSpec(
        name="record_side_effects",
        description=(
            "Set the user's side effects for TODAY. Severity is 1 (mild) to 5"
            " (severe) — higher is worse, the opposite of the check-in scale."
            " This REPLACES today's whole list, so include every effect the user"
            " currently has, not just the new one: read retrieve_medical_log first"
            " and carry the existing ones over, or you will silently delete them."
            " Only record what the user reported in their own words."
            " Do not use this to interpret a symptom, and if what they describe"
            " sounds urgent, point them to their clinician instead of logging it." + _CONFIRM_NOTE
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "effects": {
                    "type": "array",
                    "description": "Today's complete set of side effects.",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "effect": {"type": "string", "enum": list(writes.SIDE_EFFECTS)},
                            "severity": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": writes.MAX_SEVERITY,
                                "description": "1 is mild, 5 is severe.",
                            },
                        },
                        "required": ["effect", "severity"],
                    },
                },
                "note": {
                    "type": "string",
                    "maxLength": writes.MAX_NOTE_CHARS,
                    "description": "The user's own words about the day. Optional.",
                },
                CONFIRM_KEY: CONFIRM_PROPERTY,
            },
            "required": ["effects"],
        },
        handler=writes.record_side_effects,
        aliases=("record-side-effects", "log-side-effects"),
        writes=True,
        confirm_summary=_side_effects_summary,
    ),
    ToolSpec(
        name="set_todo",
        description=(
            "Create a to-do, or edit an existing one by passing its todo_id from"
            " retrieve_todos. An edit REPLACES every field, so restate the values"
            " that are not changing — read retrieve_todos first."
            " A 'daily' to-do repeats and must not have a due_date; a 'once' to-do"
            " needs one. Reminder time is the user's own local time." + _CONFIRM_NOTE
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {
                    "type": "string",
                    "maxLength": writes.MAX_TITLE_CHARS,
                    "description": "What the reminder says, in the user's words.",
                },
                "category": {"type": "string", "enum": list(writes.TODO_CATEGORIES)},
                "repeat_rule": {"type": "string", "enum": list(writes.TODO_REPEAT_RULES)},
                "remind_hour": {"type": "integer", "minimum": 0, "maximum": 23},
                "remind_minute": {"type": "integer", "minimum": 0, "maximum": 59},
                "due_date": {
                    "type": "string",
                    "format": "date",
                    "description": "Required for 'once', forbidden for 'daily'.",
                },
                "todo_id": {
                    "type": "string",
                    "description": "Only when editing. Must come from retrieve_todos.",
                },
                CONFIRM_KEY: CONFIRM_PROPERTY,
            },
            "required": ["title", "category", "repeat_rule", "remind_hour", "remind_minute"],
        },
        handler=writes.set_todo,
        aliases=("set-todo", "add-todo"),
        writes=True,
        confirm_summary=_set_todo_summary,
    ),
    ToolSpec(
        name="complete_todo",
        description=(
            "Tick a to-do as done for today, or untick it with done=false. The"
            " todo_id must come from retrieve_todos." + _CONFIRM_NOTE
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "todo_id": {"type": "string", "description": "From retrieve_todos."},
                "done": {
                    "type": "boolean",
                    "description": "Defaults to true. Pass false to untick it.",
                },
                CONFIRM_KEY: CONFIRM_PROPERTY,
            },
            "required": ["todo_id"],
        },
        handler=writes.complete_todo,
        aliases=("complete-todo",),
        writes=True,
        confirm_summary=_complete_todo_summary,
    ),
    ToolSpec(
        name="remove_todo",
        description=(
            "Delete a to-do the user no longer wants. This cannot be undone from"
            " chat. The todo_id must come from retrieve_todos." + _CONFIRM_NOTE
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "todo_id": {"type": "string", "description": "From retrieve_todos."},
                CONFIRM_KEY: CONFIRM_PROPERTY,
            },
            "required": ["todo_id"],
        },
        handler=writes.remove_todo,
        aliases=("remove-todo", "delete-todo"),
        writes=True,
        confirm_summary=_remove_todo_summary,
    ),
)
