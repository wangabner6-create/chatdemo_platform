"""Configuration sourced entirely from the environment (design D9: single axis, one provider, two API styles).

Every model-related knob — completion vs. response mode, the model name, the
endpoint, and the credentials — lives in an env var rather than being baked into
code. If neither an endpoint nor a key is present, the platform drops back to the
no-credentials-needed MockModelClient so local runs still work.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .runner.base import ModelSettings


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _csv(name: str) -> list[str]:
    raw = _env(name)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _optional_bool(name: str) -> bool | None:
    raw = _env(name).strip().lower()
    if not raw:
        return None
    return raw not in ("off", "0", "false", "no")


@dataclass
class Settings:
    # Model selection is broken out per agent role. CHATDEMO_MODEL is the only
    # mandatory setting; anything role-specific that's left unset inherits it, and
    # likewise each role's API style (completion|response) inherits CHATDEMO_API_STYLE
    # unless overridden. Mapping of role to its env var:
    #   CHATDEMO_MODEL          -> required baseline, used wherever a role has no override
    #   CHATDEMO_ROUTER_MODEL   -> the router that triages incoming turns
    #   CHATDEMO_SKILL_MODEL    -> shared across all skill agents
    #   CHATDEMO_WF_MODEL       -> the model workflow steps run on
    #   CHATDEMO_RELATED_MODEL  -> generates the related-questions suggestions
    api_style: (
        str  # CHATDEMO_API_STYLE: default style (completion|response) for roles without their own
    )
    model: str  # CHATDEMO_MODEL: baseline model, used as the fallback for every role
    router_model: str  # CHATDEMO_ROUTER_MODEL: the triage router's model
    router_api_style: str  # CHATDEMO_ROUTER_API_STYLE
    skill_model: str  # CHATDEMO_SKILL_MODEL: model shared by all skill agents
    skill_api_style: str  # CHATDEMO_SKILL_API_STYLE
    wf_model: str  # CHATDEMO_WF_MODEL: model used to run workflow steps
    wf_api_style: str  # CHATDEMO_WF_API_STYLE (unmodified passthrough: "" leaves each workflow manifest's own api_style alone)
    related_model: str  # CHATDEMO_RELATED_MODEL: model for the related-questions agent (design D15)
    related_api_style: str  # CHATDEMO_RELATED_API_STYLE
    base_url: str  # OpenAI-compatible endpoint URL; leave blank to use the mock client instead
    api_key: str
    max_tokens: int  # CHATDEMO_MAX_TOKENS: ceiling on completion output length
    enable_thinking: (
        bool | None
    )  # toggles the provider's chat-template "thinking" mode; None defers to the provider's own default
    hosted_tools: list[str]  # only meaningful in response mode (e.g. web_search)
    websearch_tool: (
        str  # which Responses web-search tool variant to use (web_search | web_search_preview)
    )
    safety: bool  # whether the LLM-based input safety guardrail is active
    scripts_enabled: bool  # whether skills may execute their bundled scripts/ via subprocess
    script_timeout: int  # wall-clock ceiling per script invocation, in seconds
    agents_db: str  # filesystem path to the SQLite DB used for sessions when Redis isn't configured
    redis_url: str  # Redis connection string for session + pending-approval storage (blank means SQLite plus in-memory)
    session_ttl: (
        int  # how long a Redis session's sliding expiry lasts, in seconds (0 disables expiry)
    )
    pending_ttl: int  # expiry for a pending approval stored in Redis, in seconds
    enabled: list[
        str
    ]  # restrict to these workflow/skill names only (empty means everything is available)
    route: str  # bypass the router and pin every turn to one handler (blank keeps normal triage)
    admin_token: str  # CHATDEMO_ADMIN_TOKEN: secret required to reach the read-only admin endpoints (blank disables them)
    tracing: bool  # export structured workflow traces to Phoenix when enabled
    phoenix_endpoint: str  # Phoenix OTLP/HTTP trace collector endpoint
    trace_project: str  # Phoenix project used to group ChatDemo traces

    @classmethod
    def from_env(cls) -> Settings:
        model = _env("CHATDEMO_MODEL", "mock")
        base_style = _env("CHATDEMO_API_STYLE", "completion")
        # Using `or model` / `or base_style` matters because compose sets these vars
        # to "" rather than leaving them absent when blank; a check for "unset only"
        # would treat that "" as a real value and pass an empty model/style through,
        # breaking the call. This way an empty string falls back just like a missing var.
        _m = lambda name: _env(name) or model
        _s = lambda name: _env(name) or base_style
        return cls(
            api_style=base_style,
            model=model,
            router_model=_m("CHATDEMO_ROUTER_MODEL"),
            router_api_style=_s("CHATDEMO_ROUTER_API_STYLE"),
            skill_model=_m("CHATDEMO_SKILL_MODEL"),
            skill_api_style=_s("CHATDEMO_SKILL_API_STYLE"),
            wf_model=_m("CHATDEMO_WF_MODEL"),
            # No fallback applied here: an empty value means don't touch whatever
            # api_style each workflow's manifest already declares; only set this var
            # if you want to override the style across every workflow at once.
            wf_api_style=_env("CHATDEMO_WF_API_STYLE"),
            related_model=_m("CHATDEMO_RELATED_MODEL"),
            related_api_style=_s("CHATDEMO_RELATED_API_STYLE"),
            base_url=_env("CHATDEMO_BASE_URL"),
            api_key=_env("CHATDEMO_API_KEY"),
            max_tokens=max(1, int(_env("CHATDEMO_MAX_TOKENS", "2048"))),
            enable_thinking=_optional_bool("CHATDEMO_ENABLE_THINKING"),
            hosted_tools=_csv("CHATDEMO_HOSTED_TOOLS"),
            websearch_tool=_env("CHATDEMO_WEBSEARCH_TOOL", "web_search"),
            safety=_env("CHATDEMO_SAFETY", "on").lower() not in ("off", "0", "false"),
            scripts_enabled=_env("CHATDEMO_SKILL_SCRIPTS", "on").lower()
            not in ("off", "0", "false"),
            script_timeout=int(_env("CHATDEMO_SCRIPT_TIMEOUT", "20")),
            agents_db=_env("CHATDEMO_AGENTS_DB", ".agents_sessions.db"),
            redis_url=_env("CHATDEMO_REDIS_URL"),
            session_ttl=int(_env("CHATDEMO_SESSION_TTL", "2592000")),  # defaults to 30 days
            pending_ttl=int(_env("CHATDEMO_PENDING_TTL", "3600")),  # defaults to 1 hour
            enabled=_csv("CHATDEMO_ENABLED"),
            route=_env("CHATDEMO_ROUTE"),
            admin_token=_env("CHATDEMO_ADMIN_TOKEN"),
            tracing=_env("CHATDEMO_TRACING", "off").lower() not in ("off", "0", "false", "no"),
            phoenix_endpoint=_env("CHATDEMO_PHOENIX_ENDPOINT", "http://127.0.0.1:6006/v1/traces"),
            trace_project=_env("CHATDEMO_TRACE_PROJECT", "chatdemo"),
        )

    @property
    def use_mock(self) -> bool:
        return not self.base_url

    def completion_params(self) -> dict[str, object]:
        params: dict[str, object] = {"max_tokens": self.max_tokens}
        if self.enable_thinking is not None:
            params["extra_body"] = {
                "chat_template_kwargs": {"enable_thinking": self.enable_thinking}
            }
        return params

    def model_settings(self) -> ModelSettings:
        return ModelSettings(
            model=self.model,
            api_style=self.api_style,
            params=self.completion_params() if self.api_style == "completion" else {},
            hosted_tools=self.hosted_tools,
        )

    def related_settings(self) -> ModelSettings:
        return ModelSettings(
            model=self.related_model,
            api_style=self.related_api_style,
            params=self.completion_params() if self.related_api_style == "completion" else {},
        )
