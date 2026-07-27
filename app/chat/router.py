"""Slash-command parsing: the deterministic, zero-token entry path.

A query that starts with `/` is a command from the app — typed by the user or
fired by a UI button. It is matched against `tools.COMMAND_INDEX` and dispatched
directly, so no LLM is involved and nothing is inferred. A query that does not
start with `/` is free text and belongs to the conversational path.

Arguments are `key=value` pairs coerced and validated against the tool's own
input schema. Validation is stricter here than on the conversational path on
purpose: a command is a contract, so a typo should be a clear 400 rather than a
silently different query.
"""

import logging
import shlex
from dataclasses import dataclass

from fastapi import HTTPException

from . import handlers, tools

logger = logging.getLogger("scan.chat.router")

COMMAND_PREFIX = "/"


@dataclass(frozen=True)
class ParsedCommand:
    spec: tools.ToolSpec
    arguments: dict


def looks_like_command(query: str) -> bool:
    return query.strip().startswith(COMMAND_PREFIX)


def _known_commands() -> str:
    return ", ".join(sorted(f"/{slug}" for slug in tools.COMMAND_INDEX))


def _coerce(spec: tools.ToolSpec, key: str, raw: str) -> object:
    """Turns a command-line string into the type the schema declares."""
    schema = spec.properties[key]
    declared = schema.get("type")

    if declared == "integer":
        try:
            value: object = int(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{key} must be a whole number.") from None
    elif declared == "number":
        try:
            value = float(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{key} must be a number.") from None
    elif declared == "boolean":
        lowered = raw.strip().lower()
        if lowered not in ("true", "false", "1", "0", "yes", "no"):
            raise HTTPException(status_code=400, detail=f"{key} must be true or false.")
        value = lowered in ("true", "1", "yes")
    elif declared == "array":
        value = [item.strip() for item in raw.split(",") if item.strip()]
    elif schema.get("format") == "date":
        # Validate here as well as in the handler, so a mistyped date is rejected
        # before the route persists anything — the same treatment an unknown
        # command gets. One shared implementation, two call sites.
        value = handlers.iso_date(raw, key)
    else:
        value = raw

    _check_enum(key, value, schema)
    return value


def _check_enum(key: str, value: object, schema: dict) -> None:
    """Enum membership, for a scalar or every member of an array."""
    if schema.get("type") == "array":
        allowed = schema.get("items", {}).get("enum")
        candidates = value if isinstance(value, list) else []
    else:
        allowed = schema.get("enum")
        candidates = [value]
    if not allowed:
        return
    for candidate in candidates:
        if candidate not in allowed:
            raise HTTPException(
                status_code=400,
                detail=f"{key} must be one of: {', '.join(map(str, allowed))}.",
            )


def parse_command(query: str) -> ParsedCommand | None:
    """`None` when the query is not a command at all (conversational path).

    Raises 400 when it *is* a command but the slug is unknown or the arguments
    do not fit the tool's schema — an unrecognised command must never fall
    through to a paid LLM call, which would answer something the user never asked.
    """
    text = query.strip()
    if not looks_like_command(text):
        return None

    try:
        tokens = shlex.split(text)
    except ValueError:
        # Unbalanced quotes; treat the words as-is rather than 500ing.
        tokens = text.split()
    if not tokens:
        return None

    slug = tools.normalize_slug(tokens[0])
    spec = tools.COMMAND_INDEX.get(slug)
    if spec is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown command /{slug}. Available commands: {_known_commands()}.",
        )

    arguments: dict = {}
    for token in tokens[1:]:
        if "=" not in token:
            raise HTTPException(
                status_code=400,
                detail=(f"Arguments to /{slug} look like key=value. Got {token!r}."),
            )
        key, _, raw = token.partition("=")
        key = key.strip().replace("-", "_").lower()
        if key not in spec.properties:
            accepted = ", ".join(sorted(spec.properties)) or "none"
            raise HTTPException(
                status_code=400,
                detail=f"/{slug} has no argument {key!r}. Accepted: {accepted}.",
            )
        arguments[key] = _coerce(spec, key, raw)

    return ParsedCommand(spec=spec, arguments=arguments)
