"""Common building blocks used by handlers: the runtime dependency container, the
Step protocol, and a Retriever protocol (retrieval isn't tied to any particular
transport — MCP is just one way to implement it)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ..contracts import RequestContext, Source
from ..runner.base import Runner

if TYPE_CHECKING:
    from ..tools_loading import LoadedTool


@runtime_checkable
class Retriever(Protocol):
    """Contract for read-only retrieval; an MCP-backed implementation is one option."""

    async def search(self, query: str, filters: dict[str, Any]) -> list[Source]: ...


class NullRetriever:
    """Stand-in retriever that always yields an empty result; swap it out for a
    real MCP-backed retriever."""

    async def search(self, query: str, filters: dict[str, Any]) -> list[Source]:
        return []


@dataclass
class HandlerDeps:
    """Bundle of runtime handles passed into a handler for the duration of a turn.
    Kept as a plain dataclass since its members are behavioral objects (protocol
    implementations) rather than data meant to be serialized."""

    runner: Runner
    retriever: Retriever = field(default_factory=NullRetriever)
    # A lighter-weight runner used for related_questions (see design D15); when
    # unset, callers should fall back to the main `runner`.
    related_runner: Runner | None = None
    # Maps each tool name this handler registered to its owner-scoped
    # orchestration function.
    tools: dict[str, LoadedTool] = field(default_factory=dict)
    handler_id: str = ""


@runtime_checkable
class Step(Protocol):
    """One step in a workflow, which updates a shared `state` dict in place rather
    than returning a new value.

    Following the convention carried over from the reference pipeline, a step is
    expected to preprocess its inputs, invoke the Runner, and then write the
    postprocessed results back into state.
    """

    async def __call__(
        self, ctx: RequestContext, state: dict[str, Any], deps: HandlerDeps
    ) -> None: ...
