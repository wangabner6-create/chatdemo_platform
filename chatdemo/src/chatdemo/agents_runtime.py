"""Backward-compatible re-exports for the agent orchestration runtime.

Prefer importing :mod:`chatdemo.nemo_runtime` in anything new. This shim exists
purely so consumers that still `import chatdemo.agents_runtime` keep working now
that the older agent-framework implementation it used to wrap has been dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

from .contracts import Response, Source
from .nemo_runtime import (
    FALLBACK_NAME,
    AgentRequest,
    Runtime,
    Specialist,
    ToolBinding,
    _append_footer,
    build_runtime,
    content_root,
    replace_session,
    run_agent,
    run_agent_streamed,
    shutdown,
    warmup,
)


@dataclass
class ChatContext:
    """Old-style container kept alive only for read-only tool integrations."""

    user: dict
    session_id: str
    history: list[dict]
    sources: list[dict] = field(default_factory=list)


def _tool_user(ctx) -> SimpleNamespace:
    context = getattr(ctx, "context", None)
    user = (getattr(context, "user", None) or {}) if context else {}
    return SimpleNamespace(
        id=user.get("id"),
        name=user.get("name"),
        session_id=getattr(context, "session_id", None) if context else None,
        sources=getattr(context, "sources", None) if context else None,
    )


def _with_context_sources(response: Response, context: ChatContext | None) -> Response:
    collected = getattr(context, "sources", None) if context else None
    if response.sources or not collected:
        return response
    response.sources = [
        source if isinstance(source, Source) else Source(**source) for source in collected
    ]
    return response


__all__ = [
    "FALLBACK_NAME",
    "AgentRequest",
    "ChatContext",
    "Runtime",
    "Specialist",
    "ToolBinding",
    "_append_footer",
    "_tool_user",
    "_with_context_sources",
    "build_runtime",
    "content_root",
    "replace_session",
    "run_agent",
    "run_agent_streamed",
    "shutdown",
    "warmup",
]
