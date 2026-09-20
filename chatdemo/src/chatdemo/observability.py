"""Observability layer: a single trace stream that drives both the server log and the SSE feed to the UI.

Every significant moment in a turn — a router handoff, a tool/skill invocation,
a workflow kicking off, each step within it — produces exactly one trace
*event*. Calling `emit()` fans that event out two ways:

1. It writes a human-readable line through the `chatdemo` logger, so watching
   the server console shows the turn unfolding live (raise the detail level
   with `CHATDEMO_LOG_LEVEL=DEBUG`).
2. When a per-turn sink is registered — the streaming endpoint sets one up via
   a ``ContextVar`` — the event is also pushed onto that sink's queue, which is
   how the same trace reaches the browser as Server-Sent Events.

Using a ``ContextVar`` here means code nested deep inside a workflow step or a
registered orchestration function can reach the request's SSE queue without
needing that queue threaded through every function signature along the way.
Because ``asyncio.create_task`` inherits a copy of the calling context, a sink
installed on the request's task remains visible from any workflow function it
spawns.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal

from nat.builder.context import Context, ContextState
from nat.data_models.intermediate_step import (
    IntermediateStepPayload,
    IntermediateStepType,
    StreamEventData,
    TraceMetadata,
    UsageInfo,
)
from nat.data_models.token_usage import TokenUsageBaseModel

log = logging.getLogger("chatdemo")

# Holds the current turn's SSE sink, if any; stays None outside of a streaming request, in which case events only go to the log.
_sink: ContextVar[asyncio.Queue | None] = ContextVar("chatdemo_trace_sink", default=None)

_CONFIGURED = False

TraceKind = Literal["llm", "tool", "custom"]
_STEP_TYPES: dict[TraceKind, tuple[IntermediateStepType, IntermediateStepType]] = {
    "llm": (IntermediateStepType.LLM_START, IntermediateStepType.LLM_END),
    "tool": (IntermediateStepType.TOOL_START, IntermediateStepType.TOOL_END),
    "custom": (IntermediateStepType.CUSTOM_START, IntermediateStepType.CUSTOM_END),
}


@dataclass
class TraceStep:
    """Mutable result handle for one native NeMo/Phoenix child span."""

    name: str
    kind: TraceKind
    manager: Any | None = None
    step_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: float = field(default_factory=time.time)
    attributes: dict[str, Any] = field(default_factory=dict)
    output: Any = None
    tokens: int | None = None

    def set_output(self, value: Any) -> None:
        self.output = value

    def set_tokens(self, value: int | None) -> None:
        self.tokens = value

    def close(self, error: BaseException | None = None) -> None:
        if self.manager is None:
            return
        metadata = dict(self.attributes)
        metadata["status"] = "error" if error else "ok"
        if error:
            metadata["error.type"] = type(error).__name__
            metadata["error.message"] = _summarize(str(error))
        usage = (
            UsageInfo(token_usage=TokenUsageBaseModel(total_tokens=self.tokens))
            if self.tokens is not None
            else None
        )
        try:
            self.manager.push_intermediate_step(
                IntermediateStepPayload(
                    UUID=self.step_id,
                    event_type=_STEP_TYPES[self.kind][1],
                    name=self.name,
                    span_event_timestamp=self.started_at,
                    metadata=TraceMetadata(provided_metadata=metadata),
                    data=StreamEventData(output=self.output),
                    usage_info=usage,
                )
            )
        except Exception:
            log.debug("failed to close native trace step %s", self.name, exc_info=True)


@contextmanager
def trace_step(
    name: str,
    kind: TraceKind = "custom",
    *,
    attributes: dict[str, Any] | None = None,
    input_value: Any = None,
) -> Iterator[TraceStep]:
    """Create a nested native NeMo span when a workflow trace is active.

    Outside a running workflow (including tests and startup) this degrades to a
    no-op handle. Export failures are likewise isolated from the user request.
    """

    handle = TraceStep(name=name, kind=kind, attributes=dict(attributes or {}))
    try:
        state = ContextState.get()
        if state.event_stream.get() is not None:
            handle.manager = Context.get().intermediate_step_manager
            handle.manager.push_intermediate_step(
                IntermediateStepPayload(
                    UUID=handle.step_id,
                    event_type=_STEP_TYPES[kind][0],
                    name=name,
                    metadata=TraceMetadata(provided_metadata=handle.attributes),
                    data=StreamEventData(input=input_value),
                )
            )
    except Exception:
        handle.manager = None
        log.debug("failed to start native trace step %s", name, exc_info=True)

    error: BaseException | None = None
    try:
        yield handle
    except BaseException as exc:
        error = exc
        raise
    finally:
        handle.close(error)


def configure_logging() -> None:
    """Attach a human-friendly console handler to the `chatdemo` logger; safe to call more than once.

    The log level is read from CHATDEMO_LOG_LEVEL, defaulting to INFO. Meant to be
    invoked a single time when the app starts up.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    level = os.environ.get("CHATDEMO_LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s", datefmt="%H:%M:%S")
    )
    log.handlers.clear()
    log.addHandler(handler)
    log.setLevel(getattr(logging, level, logging.INFO))
    log.propagate = False
    _CONFIGURED = True
    log.info("logging configured at level %s", level)


def set_sink(queue: asyncio.Queue | None):
    """Attach (or, passing None, remove) the SSE queue for the current turn.
    Hand the returned token to `reset_sink` inside a finally block."""
    return _sink.set(queue)


def reset_sink(token) -> None:
    _sink.reset(token)


def _summarize(value: Any, limit: int = 200) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = " ".join(
        text.split()
    )  # squash whitespace and line breaks so it prints on a single log line
    return text if len(text) <= limit else text[: limit - 1] + "…"


def emit(kind: str, **fields: Any) -> None:
    """Log one trace event and, if a sink is currently active, relay it to the SSE stream too.

    `kind` identifies the event category the UI branches on (e.g. "handoff",
    "tool", "workflow", "step"); everything else in `fields` is detail specific
    to that event.
    """
    event = {"type": "trace", "kind": kind, **fields}

    # -- compact, human-scannable log line --------------------------------------
    detail = " ".join(
        f"{k}={_summarize(v)}" for k, v in fields.items() if v is not None and k != "session_id"
    )
    log.info("· %-9s %s", kind, detail)

    # -- best-effort SSE relay; must never hold up the turn ---------------------
    queue = _sink.get()
    if queue is not None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:  # pragma: no cover - queue is unbounded in real usage
            log.warning("trace sink full, dropping %s event", kind)
