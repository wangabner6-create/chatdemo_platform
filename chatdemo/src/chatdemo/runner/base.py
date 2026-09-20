"""Defines the Runner contract and the model-client protocol underneath it.

Runner exists to flatten model invocation into one consistent shape no matter
which API style is in play, so handler code doesn't need to branch on
completion vs. response. Per design D9, Runners are stateless — the full
conversation input is resent on every turn rather than relying on a
provider-side thread. Because action tools are the orchestration layer's
concern, a workflow step's model calls stay limited to plain text / structured
JSON in and out.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ..contracts import Message, Source


class ModelSettings(BaseModel):
    model: str = "mock"
    api_style: str = "completion"  # one of "completion" | "response"
    params: dict[str, Any] = Field(default_factory=dict)
    # only meaningful in response mode: opt-in provider-hosted tools such as web search
    hosted_tools: list[str] = Field(default_factory=list)


class RunResult(BaseModel):
    text: str = ""
    sources: list[Source] = Field(default_factory=list)  # collected from any hosted tools that ran
    tokens: int | None = None


@runtime_checkable
class ModelClient(Protocol):
    """Represents a binding to a specific provider. Axis 1 means a single provider
    exposes two API-style methods; a concrete implementation (say, an SDK
    wrapper) provides both, and the Runner decides which one to invoke."""

    async def complete(
        self,
        *,
        model: str,
        messages: list[Message],
        response_format: dict[str, Any] | None,
        params: dict[str, Any],
    ) -> RunResult: ...

    async def respond(
        self,
        *,
        model: str,
        messages: list[Message],
        hosted_tools: list[str],
        response_format: dict[str, Any] | None,
        params: dict[str, Any],
    ) -> RunResult: ...


class Runner(Protocol):
    """The shared interface that both API-style implementations conform to."""

    async def run(
        self,
        messages: list[Message],
        *,
        response_format: dict[str, Any] | None = None,
    ) -> RunResult: ...


class ModelRunner:
    """The one concrete Runner implementation: routes to either the client's
    completion or response method based on `settings.api_style`. When running
    in response mode, it additionally passes along any provider-hosted tools
    (e.g. web_search)."""

    def __init__(self, client: ModelClient, settings: ModelSettings) -> None:
        self._client = client
        self._settings = settings

    @property
    def settings(self) -> ModelSettings:
        """Returns the (model, api_style) pair this runner is configured to dispatch on."""
        return self._settings

    async def run(
        self,
        messages: list[Message],
        *,
        response_format: dict[str, Any] | None = None,
    ) -> RunResult:
        s = self._settings
        if s.api_style == "response":
            return await self._client.respond(
                model=s.model,
                messages=messages,
                hosted_tools=s.hosted_tools,
                response_format=response_format,
                params=s.params,
            )
        return await self._client.complete(
            model=s.model,
            messages=messages,
            response_format=response_format,
            params=s.params,
        )
