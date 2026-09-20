"""A ModelClient stand-in that needs no API credentials, meant for local dev and test runs.

Because its output is deterministic, the entire platform can be exercised and
verified end to end without wiring up an actual provider. Swap it out later
for a genuine SDK-backed implementation that honors the same ModelClient
protocol.
"""

from __future__ import annotations

import json
from typing import Any

from ..contracts import Message, Source
from .base import RunResult


class MockModelClient:
    """Hands back a fixed, pre-baked result. When callers ask for a
    response_format (JSON schema), it fabricates a bare-minimum JSON object
    that satisfies the shape so downstream structured-output steps keep running."""

    async def complete(
        self,
        *,
        model: str,
        messages: list[Message],
        response_format: dict[str, Any] | None,
        params: dict[str, Any],
    ) -> RunResult:
        return self._render(messages, response_format)

    async def respond(
        self,
        *,
        model: str,
        messages: list[Message],
        hosted_tools: list[str],
        response_format: dict[str, Any] | None,
        params: dict[str, Any],
    ) -> RunResult:
        result = self._render(messages, response_format)
        if hosted_tools:  # simulate a hosted search call yielding a single source
            result.sources = [Source(title="mock hosted result", url="https://example.invalid")]
        return result

    @staticmethod
    def _render(messages: list[Message], response_format: dict[str, Any] | None) -> RunResult:
        last_user = next((m.content for m in reversed(messages) if m.role.value == "user"), "")
        if response_format is not None:
            schema = response_format.get("schema", {})
            props = schema.get("properties", {})
            obj: dict[str, Any] = {}
            for key, spec in props.items():
                t = spec.get("type")
                obj[key] = [] if t == "array" else ("" if t == "string" else None)
            return RunResult(text=json.dumps(obj), tokens=0)
        return RunResult(text=f"[mock answer] {last_user}", tokens=0)
