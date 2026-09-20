"""Model-provider adapter consumed by ChatDemo's registered orchestration functions.

Routing and dispatch live in the orchestration toolkit; what this module adds is
the actual model channel — used both for routing decisions and for model-driven
skills — talking to the same OpenAI-compatible endpoint ChatDemo already exposes
via its environment-based configuration.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from .config import Settings
from .contracts import Message, PendingAction, Role, Source
from .observability import trace_step
from .runner import build_runner
from .runner.base import ModelClient, ModelSettings
from .runner.mock import MockModelClient
from .runner.openai_sdk import OpenAISDKClient


@dataclass(frozen=True)
class ModelTool:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolOutcome:
    output: str = ""
    sources: list[Source] = field(default_factory=list)
    pending: PendingAction | None = None


@dataclass
class AgentModelResult:
    text: str = ""
    sources: list[Source] = field(default_factory=list)
    pending: PendingAction | None = None
    tokens: int | None = None
    tool_calls: int = 0


ToolInvoker = Callable[[str, dict[str, Any]], Awaitable[ToolOutcome]]


def build_model_client(settings: Settings) -> ModelClient:
    if settings.use_mock:
        return MockModelClient()
    return OpenAISDKClient(settings.base_url, settings.api_key, settings.websearch_tool)


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _message_dicts(system: str, history: list[Message], query: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    messages.extend({"role": message.role.value, "content": message.content} for message in history)
    messages.append({"role": "user", "content": query})
    return messages


class AgentModel:
    """Lightweight, provider-agnostic tool-calling loop invoked from registered orchestration functions."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = (
            None
            if settings.use_mock
            else AsyncOpenAI(
                base_url=settings.base_url or None, api_key=settings.api_key or "unset"
            )
        )

    async def choose_route(
        self,
        query: str,
        history: list[Message],
        routes: dict[str, str],
    ) -> str | None:
        if self._settings.use_mock:
            return None
        route_names = sorted(routes)
        schema = {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "properties": {"route": {"type": "string", "enum": route_names}},
                "required": ["route"],
                "additionalProperties": False,
            },
        }
        catalog = "\n".join(f"- {name}: {routes[name]}" for name in route_names)
        prompt = (
            "Route the newest user message to exactly one specialist. Use prior turns "
            "for follow-ups that refer to the previous answer. Return only the schema.\n\n"
            "Prefer a specialist whose description plausibly covers the request, even if "
            "the message is short or terse — a few-word request like 'search X' or "
            "'look up Y' is a clear match for a specialist that can search or answer "
            "general questions, not an ambiguous one. Reserve any general-purpose "
            "fallback specialist strictly for greetings, questions about what this "
            "assistant can do, and requests that no specialist's description covers at "
            "all.\n\n"
            f"Specialists:\n{catalog}"
        )
        runner = build_runner(
            ModelSettings(
                model=self._settings.router_model,
                api_style=self._settings.router_api_style,
                params=(
                    self._settings.completion_params()
                    if self._settings.router_api_style == "completion"
                    else {}
                ),
            ),
            build_model_client(self._settings),
        )
        with trace_step(
            "router.choose_route",
            "llm",
            attributes={
                "agent.role": "router",
                "llm.model_name": self._settings.router_model,
                "llm.api_style": self._settings.router_api_style,
                "route.candidates": route_names,
            },
        ) as span:
            result = await runner.run(
                [
                    Message(role=Role.SYSTEM, content=prompt),
                    *history,
                    Message(role=Role.USER, content=query),
                ],
                response_format=schema,
            )
            span.set_tokens(result.tokens)
            try:
                route = json.loads(result.text).get("route")
            except (json.JSONDecodeError, AttributeError):
                route = None
            selected = route if route in routes else None
            span.set_output({"route": selected, "parsed": route is not None})
            return selected

    async def run(
        self,
        *,
        model: str,
        api_style: str,
        system: str,
        history: list[Message],
        query: str,
        tools: list[ModelTool],
        invoke_tool: ToolInvoker,
        hosted_search: bool = False,
        max_rounds: int = 6,
    ) -> AgentModelResult:
        if self._settings.use_mock:
            return AgentModelResult(text=f"[mock answer] {query}", tokens=0)
        if api_style == "response":
            return await self._run_responses(
                model=model,
                system=system,
                history=history,
                query=query,
                tools=tools,
                invoke_tool=invoke_tool,
                hosted_search=hosted_search,
                max_rounds=max_rounds,
            )
        return await self._run_completions(
            model=model,
            system=system,
            history=history,
            query=query,
            tools=tools,
            invoke_tool=invoke_tool,
            max_rounds=max_rounds,
        )

    async def _run_completions(
        self,
        *,
        model: str,
        system: str,
        history: list[Message],
        query: str,
        tools: list[ModelTool],
        invoke_tool: ToolInvoker,
        max_rounds: int,
    ) -> AgentModelResult:
        assert self._client is not None
        messages = _message_dicts(system, history, query)
        tool_defs = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in tools
        ]
        sources: list[Source] = []
        total_tokens = 0
        tool_call_count = 0
        for round_index in range(max_rounds):
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                **self._settings.completion_params(),
            }
            if tool_defs:
                kwargs["tools"] = tool_defs
                kwargs["tool_choice"] = "auto"
            with trace_step(
                "agent.chat_completion",
                "llm",
                attributes={
                    "agent.role": "specialist",
                    "llm.model_name": model,
                    "llm.api_style": "completion",
                    "llm.round": round_index + 1,
                },
            ) as span:
                response = await self._client.chat.completions.create(**kwargs)
                round_tokens = (
                    response.usage.total_tokens
                    if response.usage and response.usage.total_tokens
                    else None
                )
                if round_tokens:
                    total_tokens += round_tokens
                message = response.choices[0].message
                calls = list(message.tool_calls or [])
                span.set_tokens(round_tokens)
                span.set_output(
                    {
                        "finish_reason": response.choices[0].finish_reason,
                        "tool_calls": [call.function.name for call in calls],
                    }
                )
            if not calls:
                return AgentModelResult(
                    text=message.content or "",
                    sources=sources,
                    tokens=total_tokens or None,
                    tool_calls=tool_call_count,
                )
            tool_call_count += len(calls)
            messages.append(message.model_dump(exclude_none=True))
            for call in calls:
                try:
                    arguments = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {"raw": call.function.arguments}
                outcome = await invoke_tool(call.function.name, arguments)
                sources.extend(outcome.sources)
                if outcome.pending:
                    return AgentModelResult(
                        pending=outcome.pending,
                        sources=sources,
                        tokens=total_tokens or None,
                        tool_calls=tool_call_count,
                    )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": outcome.output,
                    }
                )
        return AgentModelResult(
            text="I could not complete the tool sequence safely.",
            sources=sources,
            tokens=total_tokens or None,
            tool_calls=tool_call_count,
        )

    async def _run_responses(
        self,
        *,
        model: str,
        system: str,
        history: list[Message],
        query: str,
        tools: list[ModelTool],
        invoke_tool: ToolInvoker,
        hosted_search: bool,
        max_rounds: int,
    ) -> AgentModelResult:
        assert self._client is not None
        input_items: list[dict[str, Any]] = [
            {"role": message.role.value, "content": message.content} for message in history
        ]
        input_items.append({"role": "user", "content": query})
        tool_defs: list[dict[str, Any]] = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
                "strict": False,
            }
            for tool in tools
        ]
        if hosted_search:
            tool_defs.insert(0, {"type": self._settings.websearch_tool})
        sources: list[Source] = []
        total_tokens = 0
        tool_call_count = 0
        previous_response_id: str | None = None
        for round_index in range(max_rounds):
            kwargs: dict[str, Any] = {
                "model": model,
                "instructions": system,
                "input": input_items,
                "max_output_tokens": 2048,
            }
            if tool_defs:
                kwargs["tools"] = tool_defs
            if previous_response_id:
                kwargs["previous_response_id"] = previous_response_id
            with trace_step(
                "agent.response",
                "llm",
                attributes={
                    "agent.role": "specialist",
                    "llm.model_name": model,
                    "llm.api_style": "response",
                    "llm.round": round_index + 1,
                },
            ) as span:
                response = await self._client.responses.create(**kwargs)
                data = response.model_dump()
                round_tokens = int((data.get("usage") or {}).get("total_tokens") or 0)
                total_tokens += round_tokens
                sources.extend(OpenAISDKClient._extract_sources(data))
                calls = [
                    item for item in data.get("output", []) if item.get("type") == "function_call"
                ]
                span.set_tokens(round_tokens or None)
                span.set_output(
                    {
                        "response_id": response.id,
                        "tool_calls": [call.get("name", "") for call in calls],
                    }
                )
            if not calls:
                return AgentModelResult(
                    text=getattr(response, "output_text", "") or "",
                    sources=sources,
                    tokens=total_tokens or None,
                    tool_calls=tool_call_count,
                )
            tool_call_count += len(calls)
            outputs: list[dict[str, Any]] = []
            for call in calls:
                try:
                    arguments = json.loads(call.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {"raw": call.get("arguments")}
                outcome = await invoke_tool(call.get("name", ""), arguments)
                sources.extend(outcome.sources)
                if outcome.pending:
                    return AgentModelResult(
                        pending=outcome.pending,
                        sources=sources,
                        tokens=total_tokens or None,
                        tool_calls=tool_call_count,
                    )
                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.get("call_id"),
                        "output": outcome.output,
                    }
                )
            previous_response_id = response.id
            input_items = outputs
        return AgentModelResult(
            text="I could not complete the tool sequence safely.",
            sources=sources,
            tokens=total_tokens or None,
            tool_calls=tool_call_count,
        )


def stringify_tool_output(value: Any) -> str:
    return _json_text(value)
