"""Request/response models for the chat endpoint.

One response shape serves both entry paths so the client decodes a single
struct and branches on `kind` for presentation only.
"""

from pydantic import BaseModel

# Long enough for a real question, short enough that a paste-bomb can't run up
# a token bill or fill the transcript table.
MAX_QUERY_CHARS = 4000

KIND_COMMAND = "command"
KIND_CONVERSATION = "conversation"


class ChatRequest(BaseModel):
    """Extra fields are ignored, which is deliberate: a `user_id` in the body is
    never trusted. The subject is always the verified bearer token's user."""

    query: str
    thread_id: str | None = None
    # The user's assent to one previewed write: the `fingerprint` from a
    # `needs_confirmation` result, echoed back when they tap Confirm. It travels
    # on the request rather than through the conversation because the model must
    # not be able to produce it — see `confirm.py`. Absent means "not agreed",
    # which is also what a refusal looks like.
    confirm: str | None = None


class ChatToolCall(BaseModel):
    """One tool the request actually executed, with its structured result.

    Carried on both paths: the command path has exactly one, and the
    conversational path has one per tool the model chose, so the app can render
    the same cards under a natural-language answer that it renders for the
    equivalent command.
    """

    tool: str
    arguments: dict
    data: dict


class ChatThread(BaseModel):
    """One conversation in the history list."""

    id: str
    # None until the first user turn names it.
    title: str | None = None
    message_count: int = 0
    created_at: str | None = None
    updated_at: str | None = None


class ChatThreadListResult(BaseModel):
    threads: list[ChatThread] = []


class ChatMessage(BaseModel):
    """One stored turn, as the app replays a transcript."""

    id: str
    role: str
    content: str
    tool_calls: list[ChatToolCall] = []
    model: str | None = None
    prompt_version: str | None = None
    created_at: str | None = None


class ChatThreadDetail(BaseModel):
    thread_id: str
    messages: list[ChatMessage] = []


class ChatResponse(BaseModel):
    thread_id: str
    # KIND_COMMAND (direct dispatch, no LLM) or KIND_CONVERSATION.
    kind: str
    # The grounded natural-language answer; None on the command path, whose
    # result is entirely structured.
    message: str | None = None
    tool_calls: list[ChatToolCall] = []
    # Set only when an LLM was actually called, so a zero-cost command reply is
    # distinguishable from a model reply in logs and in the client.
    model: str | None = None
    prompt_version: str | None = None
    latency_ms: int
