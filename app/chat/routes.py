"""`POST /v1/chat` — the hybrid endpoint, plus the thread history routes.

Both entry paths share one spine: validate -> parse -> open thread -> log the
user turn -> produce an answer -> log the assistant turn. A slash-command
dispatches straight to its handler with no model call; free text goes to
`agent.py`, which runs the tool-calling loop over the same registry.
"""

import logging
import time

from fastapi import APIRouter, Header, HTTPException, Response

from ..config import settings
from . import agent, confirm, store, tools
from . import router as commands
from .schemas import (
    KIND_COMMAND,
    KIND_CONVERSATION,
    MAX_QUERY_CHARS,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatThread,
    ChatThreadDetail,
    ChatThreadListResult,
    ChatToolCall,
)

# Caps on the history routes: enough for a real transcript, bounded so one
# request cannot pull an unlimited amount of health-related text.
MAX_THREADS = 100
MAX_THREAD_MESSAGES = 200

logger = logging.getLogger("scan.chat")

router = APIRouter()


def _current_user(authorization: str | None) -> str:
    """Reuses `main._require_user` — the one auth implementation.

    Imported inside the function because `main` imports this router at module
    scope; a top-level import would be circular. Duplicating the auth gate here
    to avoid that would be far worse than a deferred import.
    """
    from ..main import _require_user

    return _require_user(authorization)


@router.get("/v1/chat/commands")
def chat_commands(authorization: str | None = Header(default=None)) -> dict:
    """The command catalogue, so the app renders its buttons from the registry
    instead of hardcoding slugs that drift out of sync."""
    _current_user(authorization)
    return {"commands": tools.command_catalogue()}


@router.post("/v1/chat", response_model=ChatResponse)
def chat(request: ChatRequest, authorization: str | None = Header(default=None)) -> ChatResponse:
    """Routes one turn: an explicit command dispatches directly (no LLM), free
    text goes to the model with tool-calling."""
    user_id = _current_user(authorization)
    config = settings()
    started = time.monotonic()

    query = request.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Ask a question or pick a command.")
    if len(query) > MAX_QUERY_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Keep the message under {MAX_QUERY_CHARS} characters.",
        )

    # Parse before touching the database: an unknown command should not leave a
    # thread and a dangling user turn behind.
    parsed = commands.parse_command(query)

    thread_id = store.ensure_thread(config, user_id, request.thread_id)
    store.log_message(config, user_id, thread_id, "user", query)

    if parsed is None:
        return _conversation(config, user_id, thread_id, query, started, request.confirm)

    # A write command is gated exactly like a model-issued one: the user's assent
    # token has to name a write they were previewed earlier and have not already
    # spent. Both entry paths resolve it the same way, so a slash command is no
    # shortcut around the gate.
    approved: set[str] = set()
    if parsed.spec.writes:
        approved = confirm.resolve(
            store.list_messages(config, user_id, thread_id, config.riva_chat_history_turns),
            request.confirm,
        )

    data = tools.dispatch(parsed.spec, config, user_id, parsed.arguments, approved)
    call = ChatToolCall(tool=parsed.spec.name, arguments=parsed.arguments, data=data)
    # The command path has no prose to store; the structured result is the answer.
    store.log_message(config, user_id, thread_id, "assistant", "", tool_calls=[call.model_dump()])

    latency_ms = int((time.monotonic() - started) * 1000)
    logger.info("chat command %s: thread=%s latency=%dms", parsed.spec.name, thread_id, latency_ms)
    return ChatResponse(
        thread_id=thread_id,
        kind=KIND_COMMAND,
        message=None,
        tool_calls=[call],
        latency_ms=latency_ms,
    )


def _failure_note(error: HTTPException) -> str:
    """What gets stored in place of a reply the companion could not produce.

    The user-facing detail only. A 502/503 already carries a sentence written for
    the user (`agent.UNAVAILABLE` and friends); anything else is summarised rather
    than leaked verbatim into a stored transcript.
    """
    if error.status_code in (502, 503) and isinstance(error.detail, str):
        return error.detail
    return agent.UNAVAILABLE


def _conversation(
    config,
    user_id: str,
    thread_id: str,
    query: str,
    started: float,
    assent: str | None = None,
) -> ChatResponse:
    """The free-text path: replay the thread, run the tool-calling loop, persist.

    History is read *before* the loop and excludes the turn just logged, so the
    current question is not duplicated into the prompt.
    """
    history = store.list_messages(config, user_id, thread_id, config.riva_chat_history_turns)
    # Drop the user turn this request just wrote — it is passed as the query.
    if history and history[-1].get("role") == "user":
        history = history[:-1]

    # Resolved here, not in the agent: assent comes from the request, and the
    # loop must not be able to reach anything that could manufacture it.
    approved = confirm.resolve(history, assent)

    try:
        result = agent.run(config, user_id, history, query, approved)
    except HTTPException as error:
        # The user turn is already stored. Leaving it without a reply would make
        # the thread read as though the companion ignored them, and would feed a
        # dangling question back into the next prompt.
        try:
            store.log_message(config, user_id, thread_id, "assistant", _failure_note(error))
        except Exception:
            # Best-effort bookkeeping. The model outage is what the user needs to
            # hear about; a transcript write that also failed must not replace it
            # with a different, more confusing error.
            logger.exception("could not record the failure note on thread %s", thread_id)
        raise

    calls = [ChatToolCall(**call) for call in result.tool_calls]
    store.log_message(
        config,
        user_id,
        thread_id,
        "assistant",
        result.message,
        tool_calls=[call.model_dump() for call in calls],
        model=result.model,
        prompt_version=result.prompt_version,
    )

    latency_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "chat conversation: thread=%s tools=%s iterations=%d latency=%dms",
        thread_id,
        [call.tool for call in calls],
        result.iterations,
        latency_ms,
    )
    return ChatResponse(
        thread_id=thread_id,
        kind=KIND_CONVERSATION,
        message=result.message,
        tool_calls=calls,
        model=result.model,
        prompt_version=result.prompt_version,
        latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------------
# Thread history
# ---------------------------------------------------------------------------


@router.get("/v1/chat/threads", response_model=ChatThreadListResult)
def list_threads(
    limit: int = 50, authorization: str | None = Header(default=None)
) -> ChatThreadListResult:
    """The user's conversations, most recently active first."""
    user_id = _current_user(authorization)
    rows = store.list_threads(settings(), user_id, max(1, min(limit, MAX_THREADS)))
    return ChatThreadListResult(threads=[ChatThread(**row) for row in rows])


@router.get("/v1/chat/threads/{thread_id}", response_model=ChatThreadDetail)
def get_thread(
    thread_id: str, limit: int = 100, authorization: str | None = Header(default=None)
) -> ChatThreadDetail:
    """One owned thread's turns, oldest first.

    An empty result is a 404 rather than an empty transcript: the SQL filters on
    ownership, so "no rows" and "not yours" are the same answer to the caller.
    """
    user_id = _current_user(authorization)
    thread_id = store.validated_thread_id(thread_id)
    rows = store.list_messages(
        settings(), user_id, thread_id, max(1, min(limit, MAX_THREAD_MESSAGES))
    )
    if not rows:
        raise HTTPException(status_code=404, detail=store.THREAD_GONE)
    return ChatThreadDetail(
        thread_id=thread_id,
        messages=[
            ChatMessage(
                id=row["id"],
                role=row["role"],
                content=row.get("content") or "",
                tool_calls=[ChatToolCall(**call) for call in (row.get("tool_calls") or [])],
                model=row.get("model"),
                prompt_version=row.get("prompt_version"),
                created_at=row.get("created_at"),
            )
            for row in rows
        ],
    )


@router.delete("/v1/chat/threads/{thread_id}", status_code=204)
def delete_thread(thread_id: str, authorization: str | None = Header(default=None)) -> Response:
    user_id = _current_user(authorization)
    store.delete_thread(settings(), user_id, store.validated_thread_id(thread_id))
    return Response(status_code=204)
