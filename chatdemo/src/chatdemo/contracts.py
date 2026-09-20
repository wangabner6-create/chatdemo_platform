"""Shared data contracts used throughout the platform.

These types form the boundary that lets a deterministic Workflow and a
model-driven Skill be swapped for one another transparently: any handler takes
in a `RequestContext` and hands back a `Response`, and each one publishes a
`Descriptor` that the router uses to pick between them.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Conversation turns / messages
# --------------------------------------------------------------------------- #
class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class Message(BaseModel):
    role: Role
    content: str


class User(BaseModel):
    id: str
    name: str | None = None
    entitlements: list[str] = Field(
        default_factory=list
    )  # ids of the handlers this user is permitted to invoke


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
class ToolSpec(BaseModel):
    """A typed tool description visible to the confirmation gate: name, schema, and whether it has side effects.

    Resolving the actual executor is a separate step (see tools.ToolExecutor),
    which keeps ToolSpec agnostic to transport — the underlying call could go out
    over MCP, HTTP, or just invoke a local function.
    """

    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    side_effecting: bool = True
    # Leaving this None means the gate's default behavior (require confirmation)
    # applies. Setting it to False explicitly opts a low-risk action out of that
    # requirement (design D12).
    require_confirmation: bool | None = None


class PendingAction(BaseModel):
    """A side-effecting tool call that the gate intercepted and is holding until it's confirmed."""

    tool_name: str
    arguments: dict[str, Any]
    handler_id: str


# --------------------------------------------------------------------------- #
# Response shape
# --------------------------------------------------------------------------- #
class ResponseState(str, Enum):
    ANSWER = "answer"
    CLARIFY = "clarify"
    CONFIRM = "confirm"


class Source(BaseModel):
    title: str | None = None
    url: str | None = None
    snippet: str | None = None
    score: float | None = None
    # Extra, retriever-defined fields (asset_type, coauthors, session_* and so on)
    # that enrich the answer's context internally; the UI treats this as opaque.
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResponseMeta(BaseModel):
    route: str | None = (
        None  # which handler produced this response — passed back in as previous_route on the next turn
    )
    handler_type: str | None = None
    latency_ms: float | None = None
    tokens: int | None = None
    model: str | None = None
    trace_id: str | None = None  # 32-char OpenTelemetry trace id when tracing is enabled


class Response(BaseModel):
    """The one output shape every handler returns. Because Workflow and Skill both
    produce this same structure, the streaming/UI layer never needs to special-case
    which kind of handler generated it."""

    answer: str = ""
    sources: list[Source] = Field(default_factory=list)
    related_questions: list[str] = Field(default_factory=list)
    state: ResponseState = ResponseState.ANSWER
    pending_action: PendingAction | None = None
    clarify_question: str | None = None
    meta: ResponseMeta = Field(default_factory=ResponseMeta)


# --------------------------------------------------------------------------- #
# Session state + per-request context
# --------------------------------------------------------------------------- #
class SessionState(BaseModel):
    """Conversation state that the platform (not any individual handler) owns and carries across turns and handlers."""

    session_id: str
    history: list[Message] = Field(default_factory=list)
    previous_route: str | None = None
    pending_action: PendingAction | None = None


class RequestContext(BaseModel):
    """Bundles everything a handler needs to process a single turn. Handlers hold
    no state of their own between turns — the platform tracks it and supplies it here."""

    query: str
    session: SessionState
    user: User
    product_config: dict[str, Any] = Field(default_factory=dict)
    # Live runtime objects get attached to this after construction, outside the
    # normal (de)serialization path.
    model_config = {"arbitrary_types_allowed": True}


# --------------------------------------------------------------------------- #
# Handler contract (a descriptor for discovery, an execute for the actual work)
# --------------------------------------------------------------------------- #
class Descriptor(BaseModel):
    """The metadata card, visible to the LLM, that the router uses to pick a handler."""

    id: str
    name: str
    description: str
    kind: str  # "workflow" | "skill"


@runtime_checkable
class Handler(Protocol):
    """Anything the QueryManager can pass a RequestContext to in exchange for a
    Response qualifies as a Handler — both Workflow and Skill implement it."""

    @property
    def descriptor(self) -> Descriptor: ...

    async def execute(self, ctx: RequestContext) -> Response: ...


# Signature for a tool executor: takes the call arguments plus the user making the
# call, and carries out the action.
ToolExecutor = Callable[[dict[str, Any], User], Any]
