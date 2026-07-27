"""The write gate: no health-data write happens without the user's own assent.

A read tool that guesses wrong wastes a turn. A write tool that guesses wrong
leaves a wrong number in a medical log, so the rule here is mechanical rather
than a line in the system prompt:

1. A write tool called without `confirm=true` writes **nothing** and returns a
   preview naming exactly what would be saved, plus a `fingerprint`.
2. A write tool called *with* `confirm=true` still writes nothing unless the
   user's client sent back that exact fingerprint on **this** request, it was
   previewed in an **earlier** turn of this thread, and it has not already been
   spent on a completed write.

Rule 2 is the one that matters, and the assent token is why. An earlier design
read approvals out of the stored transcript alone — but a transcript only shows
that the user was *shown* a preview, never that they agreed to it. A user who
replied "no, that's wrong" left exactly the same trace as one who replied "yes",
so a refusal was silently writable. Assent therefore arrives out-of-band, on the
request itself (`ChatRequest.confirm`), which is a field the model cannot set:
it comes from the user tapping Confirm on the preview they were shown. A "no" is
simply the absence of a token.

Fingerprints are single-use, and a turn and a thread are two separate replay
windows that need closing separately:

- **Across turns** — a completed write is stamped with its fingerprint in the
  stored `tool_calls` record, so a later request carrying the same token finds
  it spent (`consumed()`) and previews again.
- **Within one turn** — the stamp is not yet readable, and the model can emit
  the same confirmed `tool_use` block twice in a single response. So
  `tools.dispatch` also removes the fingerprint from the live `approved` set as
  it spends it. Without that, one token bought two writes.

One approval authorises one write, not an open licence to repeat it.

Arguments are fingerprinted, so a token approves the values the user was shown.
If the model comes back with a different dose or a different day, the
fingerprint misses and it previews again.
"""

import hashlib
import json
import logging

logger = logging.getLogger("scan.chat.confirm")

CONFIRM_KEY = "confirm"
NEEDS_CONFIRMATION = "needs_confirmation"
# Stamped onto a completed write's stored result so the approval cannot be
# replayed. Read back by `consumed()`.
CONFIRMED_KEY = "confirmed_fingerprint"

CONFIRM_PROPERTY = {
    "type": "boolean",
    "description": (
        "Set true ONLY after the user has seen exactly what would be saved and"
        " agreed to it in their own words. Calling without it returns a preview"
        " and writes nothing. This flag alone never authorises a write — the"
        " user's client must also return the preview's fingerprint."
    ),
}

_INSTRUCTION = (
    "Nothing was written. Tell the user in one sentence exactly what would be saved,"
    " ask them to confirm, and stop. Call this tool again with confirm=true only"
    " after they reply yes, with the same values you just showed them."
)


def fingerprint(tool: str, arguments: dict) -> str:
    """A stable id for "this tool, these values", ignoring the confirm flag."""
    payload = {key: value for key, value in arguments.items() if key != CONFIRM_KEY}
    raw = json.dumps([tool, payload], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def preview(spec, arguments: dict) -> dict:
    """The no-op result a write tool returns until the user has agreed."""
    shown = {key: value for key, value in arguments.items() if key != CONFIRM_KEY}
    return {
        "status": NEEDS_CONFIRMATION,
        "tool": spec.name,
        "fingerprint": fingerprint(spec.name, arguments),
        "will_write": spec.confirm_summary(shown),
        "arguments": shown,
        "instruction": _INSTRUCTION,
    }


def _tool_call_data(history: list[dict]):
    """Every stored tool result in the thread, oldest first."""
    for row in history:
        for call in row.get("tool_calls") or []:
            data = call.get("data")
            if isinstance(data, dict):
                yield data


def previewed_fingerprints(history: list[dict]) -> set[str]:
    """Fingerprints previewed in earlier turns, i.e. ones the user has seen.

    Being in this set means the user was *shown* the write, nothing more. It is
    a necessary condition for approval, never a sufficient one — `resolve()`
    pairs it with an assent token. Anything produced during the current turn is
    deliberately absent, so a model cannot preview and approve inside one turn.
    """
    return {
        data["fingerprint"]
        for data in _tool_call_data(history)
        if data.get("status") == NEEDS_CONFIRMATION and data.get("fingerprint")
    }


def consumed(history: list[dict]) -> set[str]:
    """Fingerprints already spent on a completed write in this thread."""
    return {data[CONFIRMED_KEY] for data in _tool_call_data(history) if data.get(CONFIRMED_KEY)}


def resolve(history: list[dict], token: str | None) -> set[str]:
    """The set of fingerprints this request may write, given the user's token.

    At most one: a request carries a single assent, so one confirmed turn
    performs one write. Returns empty for a missing, unknown, or spent token —
    every one of which means "no user agreement", and so previews again.
    """
    if not token:
        return set()
    if token not in previewed_fingerprints(history):
        logger.warning("assent token %s matches no preview in this thread", token)
        return set()
    if token in consumed(history):
        logger.warning("assent token %s was already spent on a completed write", token)
        return set()
    return {token}


def stamp(result: dict, approved_fingerprint: str) -> dict:
    """Mark a completed write's result so its approval cannot be replayed."""
    if isinstance(result, dict):
        result[CONFIRMED_KEY] = approved_fingerprint
    return result


def gate(spec, arguments: dict, approved: set[str] | None) -> dict | None:
    """`None` to proceed with the write, or the preview to return instead."""
    if not spec.writes:
        return None
    if not arguments.get(CONFIRM_KEY):
        logger.info("write tool %s previewed (no confirm flag)", spec.name)
        return preview(spec, arguments)
    if fingerprint(spec.name, arguments) not in (approved or set()):
        # Confirmed by the model, but the user did not send a matching, unspent
        # assent token for these exact values.
        logger.warning("write tool %s claimed confirmation without user assent", spec.name)
        return preview(spec, arguments)
    return None
