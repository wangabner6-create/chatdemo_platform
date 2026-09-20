"""Serializes chat output into SSE frames.

There are two code paths that both emit the same wire format (`data: {json}\\n\\n`):

- `sse_stream(events)` handles the live case: it passes through the event dicts
  coming from `run_agent_streamed` (trace/delta/meta/sources/related/state/done)
  as soon as each is produced, so the browser observes routing decisions,
  tool/skill calls, and workflow steps as they actually happen.
- `to_sse_events(resp)` handles the non-streaming case: given a `Response` that's
  already fully computed, it converts it into the identical event shapes for
  callers that only have a finished result rather than a live stream.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator

from .contracts import Response


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def sse_stream(events: AsyncIterator[dict]) -> AsyncIterator[str]:
    """Turn a live, asynchronous stream of event dicts into SSE-formatted frames."""
    async for event in events:
        yield _sse(event)


def to_sse_events(resp: Response, *, chunk_size: int = 48) -> Iterator[str]:
    yield _sse({"type": "meta", "route": resp.meta.route, "handler_type": resp.meta.handler_type})
    text = resp.answer or ""
    for i in range(0, len(text), chunk_size):
        yield _sse({"type": "delta", "text": text[i : i + chunk_size]})
    if resp.sources:
        yield _sse({"type": "sources", "sources": [s.model_dump() for s in resp.sources]})
    if resp.related_questions:
        yield _sse({"type": "related", "related_questions": resp.related_questions})
    yield _sse(
        {
            "type": "state",
            "state": resp.state.value,
            "pending_action": resp.pending_action.model_dump() if resp.pending_action else None,
            "clarify_question": resp.clarify_question,
        }
    )
    yield _sse({"type": "done"})
