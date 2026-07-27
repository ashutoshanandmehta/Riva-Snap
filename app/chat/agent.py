"""The conversational path: Claude with tool-calling over the user's own data.

The loop is the standard Messages API shape — call, and while `stop_reason` is
`tool_use`, execute the requested tools, append the assistant turn plus one user
turn carrying **all** the `tool_result` blocks, and call again. Splitting those
results across several user messages trains the model out of parallel tool use,
so they always go back together.

Two things here exist to keep a bad turn from becoming a wrong answer:

- A failing tool comes back as a `tool_result` with `is_error`, so the model can
  correct itself (a mistyped date becomes a retry, not a dead turn).
- The iteration cap ends with one `tool_choice: none` call, so the user gets an
  answer grounded in whatever the tools did return instead of nothing at all.
- A write tool is gated by `confirm.py` against the assent the user sent with
  this request, so a write they have not agreed to comes back as a preview
  instead of saving anything. The approved set is resolved by the route and
  passed in — this module never derives it, because the transcript alone cannot
  distinguish "was shown a preview" from "agreed to it".

`user_id` is bound here and passed to `tools.dispatch` positionally. It is never
part of a tool's input schema, so nothing the model emits can change whose data
is read.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException

from .. import vision
from ..config import Settings
from . import tools

logger = logging.getLogger("scan.chat.agent")

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_EFFORT = "medium"

# Generous: adaptive thinking shares this budget with the answer, and a truncated
# health answer is worse than a slow one. Well inside the non-streaming timeout.
MAX_TOKENS = 16000

UNAVAILABLE = "I could not reach the companion just now. Please try again in a moment."
REFUSED = (
    "I can't help with that one. If it's about your treatment, please bring it to your clinician."
)
NO_ANSWER = "I looked at your data but could not put an answer together. Try asking again."

# Prompt versions written before the write tools existed. They carry none of the
# confirmation rules, so pairing one with a registered write tool would put a
# model with no instructions about confirming in front of the user's medical log.
# The gate in `confirm.py` would still hold, but the prompt would be wrong about
# what the tools do, which is its own failure mode.
PRE_WRITE_PROMPT_VERSIONS = frozenset({"v1"})


@dataclass
class AgentResult:
    """What the route needs to answer and to persist the turn."""

    message: str
    tool_calls: list[dict] = field(default_factory=list)
    model: str = ""
    prompt_version: str = ""
    iterations: int = 0


def load_prompt(version: str) -> str:
    return (PROMPTS_DIR / f"companion_{version}.md").read_text()


def resolve_model(config: Settings) -> str:
    """Explicit RIVA_CHAT_MODEL override, else the Sonnet default."""
    return config.riva_chat_model or DEFAULT_MODEL


def _model_options(config: Settings, model: str) -> dict:
    """Adaptive thinking plus an effort hint, where the model supports them.

    Thinking stays **on** for this loop: it is a reasoning task, and disabling it
    on a tool-calling turn invites the model to describe a tool call in prose
    instead of emitting one. Haiku predates both parameters and errors on
    `effort`, so it runs without either — mirroring the guard in `vision.py`.
    """
    if "haiku" in model:
        return {}
    return {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": config.riva_chat_effort or DEFAULT_EFFORT},
    }


def _answer_text(response: object) -> str:
    """Every text block, joined. The model may split an answer across blocks."""
    parts = [
        block.text.strip()
        for block in response.content
        if getattr(block, "type", None) == "text" and getattr(block, "text", "").strip()
    ]
    return "\n\n".join(parts)


def history_messages(rows: list[dict]) -> list[dict]:
    """Replays stored turns as plain text messages.

    Transcripts keep prose and a `tool_calls` summary, not the raw
    `tool_use`/`tool_result` pairs, so history replays as text. That is
    deliberate: it keeps the stored shape independent of the provider's wire
    format, and the model can always re-read the data with a fresh tool call.
    """
    messages: list[dict] = []
    for row in rows:
        role = row.get("role")
        if role not in ("user", "assistant"):
            continue
        content = (row.get("content") or "").strip()
        if not content and role == "assistant":
            # A command-path turn: no prose, just structured data. Name the tools
            # so the model knows the user has already seen that result.
            names = [call.get("tool") for call in (row.get("tool_calls") or []) if call.get("tool")]
            content = f"(Showed the user {', '.join(names)} data.)" if names else ""
        if not content:
            continue
        messages.append({"role": role, "content": content})
    # The first message must be a user turn.
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    return messages


def _tool_result(tool_use_id: str, payload: object, is_error: bool = False) -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": payload if isinstance(payload, str) else json.dumps(payload, default=str),
        "is_error": is_error,
    }


def _execute(
    config: Settings, user_id: str, block: object, approved: set[str]
) -> tuple[dict, dict | None]:
    """Runs one requested tool. Returns (tool_result block, executed record).

    A failure becomes an `is_error` result rather than an exception: the model
    gets to see what went wrong and try a different argument, which is how a
    mistyped date turns into a corrected retry instead of a dead turn.
    """
    name = getattr(block, "name", "")
    arguments = getattr(block, "input", None) or {}
    spec = tools.REGISTRY.get(name)
    if spec is None:
        logger.warning("model requested unknown tool %r", name)
        return (
            _tool_result(
                block.id, f"No tool named {name}. Available: {', '.join(tools.REGISTRY)}.", True
            ),
            None,
        )
    try:
        data = tools.dispatch(spec, config, user_id, arguments, approved)
    except HTTPException as error:
        logger.info("tool %s rejected the model's arguments: %s", name, error.detail)
        return _tool_result(block.id, f"Error: {error.detail}", True), None
    except Exception as error:
        logger.exception("tool %s failed", name)
        return _tool_result(block.id, f"Error: {error}", True), None
    return (
        _tool_result(block.id, data),
        {"tool": name, "arguments": dict(arguments), "data": data},
    )


def _assert_prompt_supports_tools(prompt_version: str) -> None:
    """Refuse to run a pre-write prompt while write tools are registered.

    Loud on the first request rather than silent for the life of the misconfig:
    `config.py` documents the constraint, so it should be enforced rather than
    left as a comment someone can set `RIVA_CHAT_PROMPT_VERSION=v1` straight past.
    """
    if prompt_version not in PRE_WRITE_PROMPT_VERSIONS:
        return
    registered = sorted(name for name, spec in tools.REGISTRY.items() if spec.writes)
    if registered:
        logger.error(
            "companion prompt %s predates the write tools but %s are registered",
            prompt_version,
            registered,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                f"Companion prompt {prompt_version} predates the write tools"
                f" ({', '.join(registered)}). Use a prompt version that documents"
                " the confirmation rules, or unregister the write tools."
            ),
        )


def run(
    config: Settings,
    user_id: str,
    history: list[dict],
    query: str,
    approved: set[str] | None = None,
) -> AgentResult:
    """One conversational turn. Raises 502/503 only when the model is unreachable.

    `approved` is resolved by the route from the user's assent token; the default
    of none means every write previews rather than saves.
    """
    try:
        client = vision.make_client(config)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    model = resolve_model(config)
    prompt_version = config.riva_chat_prompt_version
    _assert_prompt_supports_tools(prompt_version)
    try:
        system_prompt = load_prompt(prompt_version)
    except OSError as error:
        raise HTTPException(
            status_code=503, detail=f"Companion prompt {prompt_version} is missing."
        ) from error

    approved = approved or set()

    messages = history_messages(history)
    messages.append({"role": "user", "content": query})
    options = _model_options(config, model)
    tool_specs = tools.anthropic_tools()
    max_iterations = max(1, config.riva_chat_max_tool_iterations)

    executed: list[dict] = []

    for iteration in range(1, max_iterations + 1):
        last = iteration == max_iterations
        # On the final pass, take tools away so the turn has to end in an answer
        # rather than another tool request the loop has no room to serve.
        choice = {"tool_choice": {"type": "none"}} if last else {}
        try:
            response = client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                tools=tool_specs,
                messages=messages,
                **options,
                **choice,
            )
        except Exception as error:
            logger.exception("companion model call failed on iteration %d", iteration)
            raise HTTPException(status_code=502, detail=UNAVAILABLE) from error

        usage = getattr(response, "usage", None)
        logger.info(
            "chat iteration %d/%d: stop=%s in=%s out=%s",
            iteration,
            max_iterations,
            getattr(response, "stop_reason", None),
            getattr(usage, "input_tokens", None),
            getattr(usage, "output_tokens", None),
        )

        stop_reason = getattr(response, "stop_reason", None)

        # Check the stop reason before reading content: a refusal carries no
        # answer, and indexing into it would raise.
        if stop_reason == "refusal":
            return AgentResult(
                message=REFUSED,
                tool_calls=executed,
                model=model,
                prompt_version=prompt_version,
                iterations=iteration,
            )

        if stop_reason != "tool_use":
            text = _answer_text(response)
            if stop_reason == "max_tokens" and text:
                text += "\n\n(That answer was cut short — ask me to continue.)"
            return AgentResult(
                message=text or NO_ANSWER,
                tool_calls=executed,
                model=model,
                prompt_version=prompt_version,
                iterations=iteration,
            )

        # Echo the assistant turn back verbatim — thinking blocks and tool_use
        # blocks both have to survive unchanged for the next call to validate.
        messages.append({"role": "assistant", "content": response.content})

        results: list[dict] = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            result, record = _execute(config, user_id, block, approved)
            results.append(result)
            if record is not None:
                executed.append(record)

        if not results:
            # stop_reason said tool_use but no block did. Nothing to answer with.
            logger.warning("tool_use stop reason with no tool_use block")
            return AgentResult(
                message=_answer_text(response) or NO_ANSWER,
                tool_calls=executed,
                model=model,
                prompt_version=prompt_version,
                iterations=iteration,
            )

        # All results for the turn in ONE user message — splitting them teaches
        # the model to stop requesting tools in parallel.
        messages.append({"role": "user", "content": results})

    # Unreachable: the last iteration runs with tool_choice "none", so it cannot
    # come back asking for another tool.
    return AgentResult(
        message=NO_ANSWER,
        tool_calls=executed,
        model=model,
        prompt_version=prompt_version,
        iterations=max_iterations,
    )
