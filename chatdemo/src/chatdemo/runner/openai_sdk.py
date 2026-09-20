"""An async ModelClient implementation that delegates to the official `openai` SDK.

- completion mode -> calls client.chat.completions.create(...)
- response mode   -> calls client.responses.create(...), wiring in hosted
  tools (e.g. web_search) and pulling sources out of citations / web_search
  output.

Picked automatically whenever a base_url is set (works against any
OpenAI-compatible endpoint). Keeps no session state between calls (per
design decision D9).

NOTE: different endpoints don't agree on the web-search response layout, so
`_extract_sources` is written defensively to handle both url_citation
annotations and plain result lists. If the shape ever changes, verify against
scripts/test_websearch.py and adjust accordingly.
"""

from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI

from ..contracts import Message, Source
from .base import RunResult

# Identifies the web-search tool variant used by the Responses API; can be swapped out via CHATDEMO_WEBSEARCH_TOOL
_HOSTED_TYPE_ALIASES = {"web_search": "web_search", "web_search_preview": "web_search_preview"}


class OpenAISDKClient:
    def __init__(self, base_url: str, api_key: str, websearch_tool: str = "web_search") -> None:
        self._client = AsyncOpenAI(base_url=base_url or None, api_key=api_key)
        self._websearch_tool = websearch_tool

    # -- chat-completions path (/chat/completions) --------------------------- #
    async def complete(
        self,
        *,
        model: str,
        messages: list[Message],
        response_format: dict[str, Any] | None,
        params: dict[str, Any],
    ) -> RunResult:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role.value, "content": m.content} for m in messages],
            **params,
        }
        if response_format is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": response_format.get("schema", {})},
            }
        resp = await self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        return RunResult(
            text=msg.content or "",
            tokens=getattr(getattr(resp, "usage", None), "total_tokens", None),
        )

    # -- responses-API path (/responses) -------------------------------------- #
    async def respond(
        self,
        *,
        model: str,
        messages: list[Message],
        hosted_tools: list[str],
        response_format: dict[str, Any] | None,
        params: dict[str, Any],
    ) -> RunResult:
        params = dict(params)
        max_out = params.pop("max_output_tokens", None) or params.pop("max_tokens", None) or 1024
        tool_defs: list[dict[str, Any]] = [
            {"type": _HOSTED_TYPE_ALIASES.get(t, self._websearch_tool)} for t in hosted_tools
        ]
        kwargs: dict[str, Any] = {
            "model": model,
            "input": [{"role": m.role.value, "content": m.content} for m in messages],
            "max_output_tokens": max_out,
            **params,
        }
        if tool_defs:
            kwargs["tools"] = tool_defs
        if response_format is not None:
            kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "output",
                    "schema": response_format.get("schema", {}),
                }
            }
        resp = await self._client.responses.create(**kwargs)
        data = resp.model_dump()
        return RunResult(
            text=getattr(resp, "output_text", "") or "",
            sources=self._extract_sources(data),
            tokens=(data.get("usage") or {}).get("total_tokens"),
        )

    # -- pulling citation/source data out of the raw response --------------- #
    @staticmethod
    def _extract_sources(data: dict) -> list[Source]:
        sources: list[Source] = []
        for item in data.get("output", []) or []:
            for block in item.get("content", []) or []:
                for ann in block.get("annotations", []) or []:
                    if ann.get("type") in ("url_citation", "citation") and ann.get("url"):
                        sources.append(Source(title=ann.get("title"), url=ann.get("url")))
            for key in ("results", "search_results", "sources"):
                for r in item.get(key, []) or []:
                    if isinstance(r, dict) and r.get("url"):
                        sources.append(Source(title=r.get("title"), url=r.get("url")))
        return sources
