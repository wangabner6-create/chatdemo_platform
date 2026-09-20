"""Content safety gate: an LLM-based guardrail every turn must pass before it gets routed onward.

Wraps an LLM classifier that emits {"category": ["safe"|"toxic"]}. When the
response can't be parsed or the provider hiccups transiently, it defaults to
letting the turn through as "safe" — but if the provider itself explicitly
rejects the prompt via its content filter, that's treated as unsafe instead.
"""

from __future__ import annotations

import json
from pathlib import Path

from .contracts import Message, Role
from .observability import trace_step
from .prompts import system_prompt
from .runner.base import Runner

_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "array",
                "items": {"type": "string", "enum": ["safe", "toxic"]},
            }
        },
        "required": ["category"],
        "additionalProperties": False,
    },
}

_FALLBACK_SYSTEM = (
    "You are a safety classification model. Classify the user query as 'toxic' or "
    '\'safe\'. Respond with JSON {"category": ["safe"]} or {"category": ["toxic"]}.'
)


def _is_content_filter(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "content_filter" in msg or "content management policy" in msg or "contentfiltered" in msg


class LLMSafetyChecker:
    """Classifies incoming queries by pairing a Runner with the bundled safety prompt."""

    def __init__(self, runner: Runner, prompts_dir: Path, model_config: str = "default") -> None:
        self._runner = runner
        self._system = system_prompt(prompts_dir, "safety", model_config) or _FALLBACK_SYSTEM

    async def check(self, query: str) -> bool:
        """Return True when the query has been judged safe."""
        with trace_step(
            "safety.check",
            "llm",
            attributes={"agent.role": "safety", "input.characters": len(query)},
        ) as span:
            try:
                result = await self._runner.run(
                    [
                        Message(role=Role.SYSTEM, content=self._system),
                        Message(role=Role.USER, content=query),
                    ],
                    response_format=_SCHEMA,
                )
            except Exception as exc:  # noqa: BLE001
                # If the provider's own content filter blocked the prompt, mark it unsafe;
                # any other (presumably transient) failure defaults open to safe, matching
                # the reference implementation's behavior.
                safe = not _is_content_filter(exc)
                span.set_output({"safe": safe, "fallback": True})
                return safe
            safe = self._is_safe(result.text)
            span.set_tokens(result.tokens)
            span.set_output({"safe": safe})
            return safe

    @staticmethod
    def _is_safe(text: str) -> bool:
        try:
            cats = json.loads(text).get("category", ["safe"])
        except (json.JSONDecodeError, AttributeError):
            return True  # can't parse it, so default to letting it through as safe
        category = (
            cats[0]
            if isinstance(cats, list) and cats
            else (cats if isinstance(cats, str) else "safe")
        )
        return category != "toxic"
