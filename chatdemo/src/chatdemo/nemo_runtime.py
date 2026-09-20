"""ChatDemo's agent orchestration runtime.

Ownership of the executable graph sits with the orchestration toolkit:

* each typed action, reference, or script tool becomes a registered function;
* each deterministic workflow and model-driven skill likewise becomes one;
* a single typed entry workflow handles safety checks, routing, confirmation,
  and dispatch;
* FastAPI is kept as nothing more than a thin transport layer over ChatDemo's
  uniform ``Response`` contract.

The deterministic workflow DAG engine stays in place by design, as domain
logic in its own right. The orchestration toolkit simply dispatches to those
existing handlers rather than re-wrapping them inside a second vendor's own
agent/handoff/session abstraction.
"""

import asyncio
import importlib.metadata
import importlib.resources as ilr
import inspect
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from nat.builder.context import Context
from nat.builder.function import Function
from nat.builder.function_info import FunctionInfo
from nat.builder.workflow import Workflow
from nat.builder.workflow_builder import WorkflowBuilder
from nat.cli.register_workflow import register_function
from nat.data_models.config import GeneralConfig, TelemetryConfig
from nat.data_models.function import FunctionBaseConfig
from pydantic import BaseModel, Field

from .config import Settings
from .contracts import (
    Message,
    PendingAction,
    RequestContext,
    Response,
    ResponseMeta,
    ResponseState,
    Role,
    SessionState,
    Source,
    ToolSpec,
    User,
)
from .nemo_models import (
    AgentModel,
    ModelTool,
    ToolOutcome,
    build_model_client,
    stringify_tool_output,
)
from .observability import emit, log, reset_sink, set_sink, trace_step
from .registry import Registry, SkillMeta, list_skills
from .runner import build_runner
from .safety import LLMSafetyChecker, _is_content_filter
from .skill_scripts import run_skill_script
from .store import ChatSafeSession, MemoryPending, RedisPending, RedisSession, SQLiteSession
from .tools_loading import LoadedTool

FALLBACK_NAME = "general-fallback"
_AFFIRM = {"yes", "y", "confirm", "ok", "okay", "sure", "yep", "是", "确认", "好"}


def content_root() -> Path:
    return Path(ilr.files("chatdemo")) / "content"


class AgentRequest(BaseModel):
    query: str
    session_id: str
    user: User = Field(default_factory=lambda: User(id="anon"))
    history: list[Message] = Field(default_factory=list)
    pending_action: PendingAction | None = None

    def request_context(self) -> RequestContext:
        return RequestContext(
            query=self.query,
            session=SessionState(session_id=self.session_id, history=self.history),
            user=self.user,
        )


class ToolRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    user: User = Field(default_factory=lambda: User(id="anon"))
    session_id: str = ""


class ToolExecution(BaseModel):
    output: str = ""
    sources: list[Source] = Field(default_factory=list)


class ChatDemoToolConfig(FunctionBaseConfig, name="chatdemo_tool"):
    owner_id: str
    tool_name: str


class ChatDemoSpecialistConfig(FunctionBaseConfig, name="chatdemo_specialist"):
    specialist_id: str
    kind: Literal["workflow", "skill"]


class ChatDemoWorkflowConfig(FunctionBaseConfig, name="chatdemo_workflow"):
    routes: list[str] = Field(default_factory=list)
    fixed_route: str = ""


@dataclass
class _ToolAsset:
    owner_id: str
    spec: ToolSpec
    kind: Literal["loaded", "reference", "script"]
    loaded: LoadedTool | None = None
    skill: SkillMeta | None = None


@dataclass
class ToolBinding:
    owner_id: str
    spec: ToolSpec
    function_name: str
    function: Function


@dataclass
class Specialist:
    name: str
    kind: Literal["workflow", "skill"]
    description: str
    instructions: str
    function_name: str
    function: Function
    tools: dict[str, ToolBinding] = field(default_factory=dict)
    model: str = "mock"
    api_style: str = "completion"
    web_search: bool = False


@dataclass(frozen=True)
class Router:
    model: str
    api_style: str


@dataclass
class Runtime:
    workflow: Workflow
    builder: WorkflowBuilder
    triage: Router
    specialists: dict[str, Specialist]
    footers: dict[str, str]
    framework: str = "nvidia-nat"
    framework_version: str = field(default_factory=lambda: importlib.metadata.version("nvidia-nat"))

    async def close(self) -> None:
        await self.builder.__aexit__(None, None, None)


@dataclass
class _BuildAssets:
    settings: Settings
    handlers: dict[str, Any]
    skills: dict[str, SkillMeta]
    descriptions: dict[str, str]
    instructions: dict[str, str]
    tools: dict[tuple[str, str], _ToolAsset]
    tool_names: dict[str, list[str]]
    model: AgentModel
    safety: LLMSafetyChecker | None


_BUILD_ASSETS: ContextVar[_BuildAssets | None] = ContextVar(
    "chatdemo_nemo_build_assets", default=None
)


def _assets() -> _BuildAssets:
    assets = _BUILD_ASSETS.get()
    if assets is None:
        raise RuntimeError("NeMo function built outside ChatDemo runtime assembly")
    return assets


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", value).strip("_").lower()


def _tool_function_name(owner_id: str, tool_name: str) -> str:
    return f"{_safe_name(owner_id)}__tool__{_safe_name(tool_name)}"


def _specialist_function_name(specialist_id: str) -> str:
    return f"{_safe_name(specialist_id)}__specialist"


def _tool_specs_for_owner(assets: _BuildAssets, owner_id: str) -> list[_ToolAsset]:
    return [assets.tools[(owner_id, name)] for name in assets.tool_names.get(owner_id, [])]


def _tool_user(request: ToolRequest) -> SimpleNamespace:
    return SimpleNamespace(
        id=request.user.id,
        name=request.user.name,
        session_id=request.session_id,
        sources=[],
    )


@register_function(config_type=ChatDemoToolConfig)
async def _register_chatdemo_tool(config: ChatDemoToolConfig, _builder):
    assets = _assets()
    asset = assets.tools[(config.owner_id, config.tool_name)]

    async def invoke(request: ToolRequest) -> ToolExecution:
        emit("nemo", event="function_start", name=config.name or config.tool_name, role="tool")
        emit("tool", event="start", name=asset.spec.name, owner=asset.owner_id)
        user = _tool_user(request)
        if asset.kind == "reference":
            assert asset.skill is not None
            name = str(request.arguments.get("name") or "")
            path = asset.skill.references.get(name) or asset.skill.references.get(Path(name).name)
            output: Any = (
                path.read_text()[:40000]
                if path and path.exists()
                else f"reference '{name}' not found"
            )
        elif asset.kind == "script":
            assert asset.skill is not None
            name = str(request.arguments.get("name") or "")
            args = request.arguments.get("args") or []
            output = run_skill_script(
                asset.skill.scripts,
                name,
                [str(arg) for arg in args],
                assets.settings.script_timeout,
            )
        else:
            assert asset.loaded is not None
            output = asset.loaded.executor(request.arguments, user)
            if inspect.isawaitable(output):
                output = await output
        sources = [
            source if isinstance(source, Source) else Source(**source)
            for source in (user.sources or [])
        ]
        text = stringify_tool_output(output)
        emit("tool", event="end", name=asset.spec.name, owner=asset.owner_id, result=text)
        emit("nemo", event="function_end", name=config.name or config.tool_name, role="tool")
        return ToolExecution(output=text, sources=sources)

    yield FunctionInfo.from_fn(invoke, description=asset.spec.description)


async def _invoke_binding(
    binding: ToolBinding,
    request: AgentRequest,
    arguments: dict[str, Any],
) -> ToolOutcome:
    if binding.spec.side_effecting and binding.spec.require_confirmation is not False:
        pending = PendingAction(
            tool_name=binding.spec.name,
            arguments=arguments,
            handler_id=binding.owner_id,
        )
        emit("confirm", tool=pending.tool_name, agent=pending.handler_id)
        return ToolOutcome(pending=pending)
    with trace_step(
        f"tool.{binding.spec.name}",
        "tool",
        attributes={"tool.name": binding.spec.name, "tool.owner": binding.owner_id},
    ) as span:
        result = await binding.function.ainvoke(
            ToolRequest(arguments=arguments, user=request.user, session_id=request.session_id),
            to_type=ToolExecution,
        )
        span.set_output(
            {"source_count": len(result.sources) if isinstance(result, ToolExecution) else 0}
        )
    if not isinstance(result, ToolExecution):
        result = ToolExecution.model_validate(result)
    return ToolOutcome(output=result.output, sources=result.sources)


def _confirm_response(pending: PendingAction) -> Response:
    return Response(
        state=ResponseState.CONFIRM,
        answer=(
            f"Please confirm: {pending.tool_name}({pending.arguments}). "
            "Reply 'yes' to proceed or 'no' to cancel."
        ),
        pending_action=pending,
        meta=ResponseMeta(route=pending.handler_id, handler_type="nemo_tool"),
    )


def _tool_answer(execution: ToolExecution, owner_id: str) -> Response:
    answer = execution.output
    try:
        value = json.loads(execution.output)
    except (json.JSONDecodeError, TypeError):
        value = None
    if isinstance(value, dict) and value.get("message"):
        answer = str(value["message"])
    return Response(
        answer=answer,
        sources=execution.sources,
        meta=ResponseMeta(route=owner_id, handler_type="nemo_tool"),
    )


@register_function(config_type=ChatDemoSpecialistConfig)
async def _register_chatdemo_specialist(config: ChatDemoSpecialistConfig, builder):
    assets = _assets()
    tool_bindings: dict[str, ToolBinding] = {}
    for asset in _tool_specs_for_owner(assets, config.specialist_id):
        function_name = _tool_function_name(config.specialist_id, asset.spec.name)
        tool_bindings[asset.spec.name] = ToolBinding(
            owner_id=config.specialist_id,
            spec=asset.spec,
            function_name=function_name,
            function=await builder.get_function(function_name),
        )

    if config.kind == "workflow":
        handler = assets.handlers[config.specialist_id]

        async def run(request: AgentRequest) -> Response:
            emit(
                "nemo",
                event="function_start",
                name=config.name or config.specialist_id,
                role="workflow_specialist",
            )
            if tool_bindings and not assets.settings.use_mock:

                async def invoke(name: str, arguments: dict[str, Any]) -> ToolOutcome:
                    binding = tool_bindings.get(name)
                    if binding is None:
                        return ToolOutcome(output=f"Unknown tool: {name}")
                    return await _invoke_binding(binding, request, arguments)

                model_tools = [
                    ModelTool(
                        name=binding.spec.name,
                        description=binding.spec.description,
                        input_schema=binding.spec.input_schema,
                    )
                    for binding in tool_bindings.values()
                ]
                settings = handler.run_model_settings
                decision = await assets.model.run(
                    model=settings.model,
                    api_style=settings.api_style,
                    system=(
                        f"You are the action gate for the {config.specialist_id} domain. "
                        "Call a tool only when the user explicitly asks to perform that "
                        "action. For an informational question, return ROUTE_TO_WORKFLOW."
                    ),
                    history=request.history,
                    query=request.query,
                    tools=model_tools,
                    invoke_tool=invoke,
                )
                if decision.pending:
                    return _confirm_response(decision.pending)
                if decision.tool_calls:
                    return Response(
                        answer=decision.text,
                        sources=decision.sources,
                        meta=ResponseMeta(
                            route=config.specialist_id,
                            handler_type="nemo_tool_agent",
                            tokens=decision.tokens,
                        ),
                    )
            response = await handler.execute(request.request_context())
            response.meta.route = config.specialist_id
            response.meta.handler_type = "nemo_workflow"
            emit(
                "nemo",
                event="function_end",
                name=config.name or config.specialist_id,
                role="workflow_specialist",
            )
            return response

    else:
        skill = assets.skills[config.specialist_id]

        async def run(request: AgentRequest) -> Response:
            emit(
                "nemo",
                event="function_start",
                name=config.name or config.specialist_id,
                role="skill",
            )

            async def invoke(name: str, arguments: dict[str, Any]) -> ToolOutcome:
                binding = tool_bindings.get(name)
                if binding is None:
                    return ToolOutcome(output=f"Unknown tool: {name}")
                return await _invoke_binding(binding, request, arguments)

            result = await assets.model.run(
                model=assets.settings.skill_model,
                api_style=("response" if skill.web_search else assets.settings.skill_api_style),
                system=assets.instructions[config.specialist_id],
                history=request.history,
                query=request.query,
                tools=[
                    ModelTool(
                        name=binding.spec.name,
                        description=binding.spec.description,
                        input_schema=binding.spec.input_schema,
                    )
                    for binding in tool_bindings.values()
                ],
                invoke_tool=invoke,
                hosted_search=skill.web_search,
            )
            if result.pending:
                return _confirm_response(result.pending)
            response = Response(
                answer=result.text,
                sources=result.sources,
                meta=ResponseMeta(
                    route=config.specialist_id,
                    handler_type="nemo_skill",
                    tokens=result.tokens,
                ),
            )
            emit(
                "nemo",
                event="function_end",
                name=config.name or config.specialist_id,
                role="skill",
            )
            return response

    yield FunctionInfo.from_fn(run, description=assets.descriptions[config.specialist_id])


def _offline_route(query: str, routes: dict[str, str]) -> str:
    lower = query.lower()
    keyword_routes = (("demo-assistant", ("demo", "help", "assistant")),)
    for route, keywords in keyword_routes:
        if route in routes and any(keyword in lower for keyword in keywords):
            return route
    if FALLBACK_NAME in routes:
        return FALLBACK_NAME
    return next(iter(routes))


def _refusal() -> Response:
    return Response(
        answer="This request can't be handled.",
        state=ResponseState.CLARIFY,
        meta=ResponseMeta(route="safety", handler_type="nemo_guardrail"),
    )


@register_function(config_type=ChatDemoWorkflowConfig)
async def _register_chatdemo_workflow(config: ChatDemoWorkflowConfig, builder):
    assets = _assets()
    specialists = {
        route: await builder.get_function(_specialist_function_name(route))
        for route in config.routes
    }
    tool_functions = {
        (owner_id, tool_name): await builder.get_function(_tool_function_name(owner_id, tool_name))
        for owner_id, names in assets.tool_names.items()
        for tool_name in names
    }

    async def run(request: AgentRequest) -> Response:
        if assets.settings.use_mock:
            return Response(
                answer=f"[mock answer] {request.query}",
                meta=ResponseMeta(route="mock", handler_type="mock", tokens=0),
            )
        if request.pending_action is not None:
            pending = request.pending_action
            approved = (
                request.query.strip().lower() in _AFFIRM
                or request.query.strip().lower().startswith("yes")
            )
            emit("resume", approved=approved, tool=pending.tool_name, agent=pending.handler_id)
            if not approved:
                return Response(
                    answer="Canceled.",
                    meta=ResponseMeta(route=pending.handler_id, handler_type="nemo_tool"),
                )
            function = tool_functions.get((pending.handler_id, pending.tool_name))
            if function is None:
                raise ValueError(
                    f"pending tool {pending.handler_id}/{pending.tool_name} is no longer enabled"
                )
            execution = await function.ainvoke(
                ToolRequest(
                    arguments=pending.arguments,
                    user=request.user,
                    session_id=request.session_id,
                ),
                to_type=ToolExecution,
            )
            if not isinstance(execution, ToolExecution):
                execution = ToolExecution.model_validate(execution)
            return _tool_answer(execution, pending.handler_id)
        if assets.safety is not None and not await assets.safety.check(request.query):
            emit("guardrail", event="blocked", name="safety")
            return _refusal()
        route = config.fixed_route or await assets.model.choose_route(
            request.query,
            request.history,
            {name: assets.descriptions[name] for name in config.routes},
        )
        if route not in specialists:
            route = _offline_route(
                request.query,
                {name: assets.descriptions[name] for name in config.routes},
            )
        emit("handoff", **{"from": "triage", "to": route})
        response = await specialists[route].ainvoke(request, to_type=Response)
        if not isinstance(response, Response):
            response = Response.model_validate(response)
        response.meta.route = response.meta.route or route
        return response

    yield FunctionInfo.from_fn(
        run,
        description="ChatDemo safety, routing, confirmation, and specialist dispatch workflow.",
    )


def _scope_catalog(descriptions: dict[str, str]) -> str:
    return "\n".join(
        f"- {name}: {' '.join(description.split())}"
        for name, description in descriptions.items()
        if name != FALLBACK_NAME and description.strip()
    )


def _skill_instructions(
    meta: SkillMeta,
    settings: Settings,
    descriptions: dict[str, str],
) -> str:
    notes: list[str] = []
    if meta.references:
        notes.append(f"Reference files (read with read_reference): {', '.join(meta.references)}")
    if settings.scripts_enabled and meta.scripts:
        notes.append(f"Runnable scripts (run with run_script): {', '.join(meta.scripts)}")
    body_runs_scripts = "run_script" in meta.instructions or "scripts/" in meta.instructions
    if meta.source and body_runs_scripts and not (settings.scripts_enabled and meta.scripts):
        notes.append(
            "GUIDANCE-ONLY IMPORT: nothing here executes and there is no run_script tool. "
            "Give the user concrete commands to run themselves. Read only the bundled "
            "reference files when exact script behavior is needed; never invent file names."
        )
    if meta.name == FALLBACK_NAME:
        catalog = _scope_catalog(descriptions)
        if catalog:
            notes.append(
                "## Current scope (auto-generated — the specialists enabled right now)\n"
                "This is the live, complete list of capabilities. Rephrase it for users; "
                "never print internal specialist names or invent capabilities.\n\n"
                f"{catalog}"
            )
    return meta.instructions + ("\n\n" + "\n\n".join(notes) if notes else "")


def _reference_spec() -> ToolSpec:
    return ToolSpec(
        name="read_reference",
        description="Read one bundled reference file by name.",
        side_effecting=False,
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    )


def _script_spec() -> ToolSpec:
    return ToolSpec(
        name="run_script",
        description="Run one allowlisted script bundled with this skill.",
        side_effecting=False,
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["name"],
        },
    )


async def build_runtime(root: Path, settings: Settings) -> Runtime:
    client = build_model_client(settings)
    override = "" if settings.use_mock else settings.wf_model
    related = None if settings.use_mock else settings.related_settings()
    handlers = Registry(
        root,
        client,
        model_override=override,
        related=related,
        wf_api_style=settings.wf_api_style,
    ).discover()
    enabled = set(settings.enabled)

    def is_enabled(name: str) -> bool:
        return not enabled or name in enabled

    handlers = {name: handler for name, handler in handlers.items() if is_enabled(name)}
    skills = {meta.name: meta for meta in list_skills(root) if is_enabled(meta.name)}
    descriptions = {
        **{name: handler.descriptor.description for name, handler in handlers.items()},
        **{name: meta.description for name, meta in skills.items()},
    }
    if not descriptions:
        raise ValueError("no ChatDemo workflows or skills are enabled")
    if settings.route and settings.route not in descriptions:
        raise ValueError(
            f"CHATDEMO_ROUTE={settings.route!r} is not an enabled handler {sorted(descriptions)}"
        )
    instructions = {
        name: _skill_instructions(meta, settings, descriptions) for name, meta in skills.items()
    }
    tools: dict[tuple[str, str], _ToolAsset] = {}
    tool_names: dict[str, list[str]] = {}
    for owner_id, handler in handlers.items():
        for loaded in handler.action_tools.values():
            tools[(owner_id, loaded.spec.name)] = _ToolAsset(
                owner_id=owner_id,
                spec=loaded.spec,
                kind="loaded",
                loaded=loaded,
            )
            tool_names.setdefault(owner_id, []).append(loaded.spec.name)
    for owner_id, skill in skills.items():
        if skill.references:
            spec = _reference_spec()
            tools[(owner_id, spec.name)] = _ToolAsset(
                owner_id=owner_id,
                spec=spec,
                kind="reference",
                skill=skill,
            )
            tool_names.setdefault(owner_id, []).append(spec.name)
        if settings.scripts_enabled and skill.scripts:
            spec = _script_spec()
            tools[(owner_id, spec.name)] = _ToolAsset(
                owner_id=owner_id,
                spec=spec,
                kind="script",
                skill=skill,
            )
            tool_names.setdefault(owner_id, []).append(spec.name)
        for loaded in skill.tools.values():
            tools[(owner_id, loaded.spec.name)] = _ToolAsset(
                owner_id=owner_id,
                spec=loaded.spec,
                kind="loaded",
                loaded=loaded,
                skill=skill,
            )
            tool_names.setdefault(owner_id, []).append(loaded.spec.name)
    safety = (
        LLMSafetyChecker(
            build_runner(settings.model_settings(), client),
            root / "prompts",
        )
        if settings.safety
        else None
    )
    assets = _BuildAssets(
        settings=settings,
        handlers=handlers,
        skills=skills,
        descriptions=descriptions,
        instructions=instructions,
        tools=tools,
        tool_names=tool_names,
        model=AgentModel(settings),
        safety=safety,
    )
    if settings.tracing:
        try:
            from nat.plugins.phoenix.register import PhoenixTelemetryExporter
        except ImportError as exc:  # pragma: no cover - exercised only in minimal installs
            raise RuntimeError(
                "CHATDEMO_TRACING is enabled, but the observability extra is not installed; "
                "run `uv sync --extra observability`."
            ) from exc
        general_config = GeneralConfig(
            telemetry=TelemetryConfig(
                tracing={
                    "phoenix": PhoenixTelemetryExporter(
                        endpoint=settings.phoenix_endpoint,
                        project=settings.trace_project,
                        batch_size=1,
                        flush_interval=1.0,
                    )
                }
            )
        )
        builder = WorkflowBuilder(general_config=general_config)
    else:
        builder = WorkflowBuilder()
    await builder.__aenter__()
    token = _BUILD_ASSETS.set(assets)
    try:
        for (owner_id, tool_name), asset in tools.items():
            await builder.add_function(
                _tool_function_name(owner_id, tool_name),
                ChatDemoToolConfig(
                    owner_id=owner_id,
                    tool_name=tool_name,
                    name=f"{owner_id}.{asset.spec.name}",
                ),
            )
        for specialist_id in descriptions:
            await builder.add_function(
                _specialist_function_name(specialist_id),
                ChatDemoSpecialistConfig(
                    specialist_id=specialist_id,
                    kind="workflow" if specialist_id in handlers else "skill",
                    name=specialist_id,
                ),
            )
        await builder.set_workflow(
            ChatDemoWorkflowConfig(
                routes=list(descriptions),
                fixed_route=settings.route,
                name="chatdemo",
            )
        )
        workflow = await builder.build()
        specialists: dict[str, Specialist] = {}
        for specialist_id, description in descriptions.items():
            function_name = _specialist_function_name(specialist_id)
            bindings: dict[str, ToolBinding] = {}
            for tool_name in tool_names.get(specialist_id, []):
                asset = tools[(specialist_id, tool_name)]
                tool_function_name = _tool_function_name(specialist_id, tool_name)
                bindings[tool_name] = ToolBinding(
                    owner_id=specialist_id,
                    spec=asset.spec,
                    function_name=tool_function_name,
                    function=await builder.get_function(tool_function_name),
                )
            if specialist_id in handlers:
                model_settings = handlers[specialist_id].run_model_settings
                kind: Literal["workflow", "skill"] = "workflow"
                specialist_instructions = description
                web_search = False
            else:
                model_settings = SimpleNamespace(
                    model=settings.skill_model,
                    api_style=(
                        "response" if skills[specialist_id].web_search else settings.skill_api_style
                    ),
                )
                kind = "skill"
                specialist_instructions = instructions[specialist_id]
                web_search = skills[specialist_id].web_search
            specialists[specialist_id] = Specialist(
                name=specialist_id,
                kind=kind,
                description=description,
                instructions=specialist_instructions,
                function_name=function_name,
                function=await builder.get_function(function_name),
                tools=bindings,
                model=model_settings.model,
                api_style=model_settings.api_style,
                web_search=web_search,
            )
        return Runtime(
            workflow=workflow,
            builder=builder,
            triage=Router(settings.router_model, settings.router_api_style),
            specialists=specialists,
            footers={name: meta.source for name, meta in skills.items() if meta.source},
        )
    except Exception:
        await builder.__aexit__(None, None, None)
        raise
    finally:
        _BUILD_ASSETS.reset(token)


_RUNTIMES: dict[str, Runtime] = {}


def _runtime_key(root: Path, settings: Settings) -> str:
    return f"{root.resolve()}::{settings!r}"


async def _cached_runtime() -> Runtime:
    root = content_root()
    settings = Settings.from_env()
    key = _runtime_key(root, settings)
    runtime = _RUNTIMES.get(key)
    if runtime is None:
        runtime = await build_runtime(root, settings)
        _RUNTIMES[key] = runtime
    return runtime


async def warmup() -> None:
    runtime = await _cached_runtime()
    log.info(
        "NeMo runtime ready version=%s specialists=%d",
        runtime.framework_version,
        len(runtime.specialists),
    )


async def shutdown() -> None:
    runtimes = list(_RUNTIMES.values())
    _RUNTIMES.clear()
    for runtime in runtimes:
        await runtime.close()


def _session(session_id: str):
    settings = Settings.from_env()
    inner = (
        RedisSession(session_id, settings.redis_url, ttl_seconds=settings.session_ttl)
        if settings.redis_url
        else SQLiteSession(session_id, settings.agents_db)
    )
    return ChatSafeSession(inner)


_PENDING_STORES: dict[tuple[str, int], Any] = {}


def _pending_store():
    settings = Settings.from_env()
    key = (settings.redis_url, settings.pending_ttl)
    if key not in _PENDING_STORES:
        _PENDING_STORES[key] = (
            RedisPending(settings.redis_url, ttl_seconds=settings.pending_ttl)
            if settings.redis_url
            else MemoryPending()
        )
    return _PENDING_STORES[key]


async def replace_session(session_id: str, messages: list[dict]) -> int:
    session = _session(session_id)
    await session.clear_session()
    items = [
        {"role": message["role"], "content": message["content"]}
        for message in messages
        if isinstance(message, dict)
        and message.get("role") in ("user", "assistant")
        and message.get("content")
    ]
    if items:
        await session.add_items(items)
    await _pending_store().pop(session_id)
    return len(items)


def _history(items: list[dict]) -> list[Message]:
    return [
        Message(role=Role(item["role"]), content=str(item["content"]))
        for item in items
        if item.get("role") in ("user", "assistant") and item.get("content")
    ]


def _append_footer(response: Response, footers: dict[str, str]) -> str:
    footer = footers.get(response.meta.route or "")
    if not footer or (response.answer and footer in response.answer):
        return ""
    suffix = ("\n\n" if response.answer else "") + footer
    response.answer = (response.answer or "") + suffix
    return suffix


async def _execute_turn(query: str, session_id: str, user: User | None) -> Response:
    runtime = await _cached_runtime()
    settings = Settings.from_env()
    session = _session(session_id)
    pending_raw = await _pending_store().pop(session_id)
    pending = PendingAction.model_validate_json(pending_raw) if pending_raw else None
    request = AgentRequest(
        query=query,
        session_id=session_id,
        user=user or User(id="anon"),
        history=_history(await session.get_items()),
        pending_action=pending,
    )
    started = time.perf_counter()
    trace_id = uuid.uuid4().hex if settings.tracing else None
    workflow_run_id = str(uuid.uuid4()) if settings.tracing else None
    log.info(
        "chat start session=%s trace=%s entry=chatdemo query=%r",
        session_id,
        trace_id or "off",
        query,
    )
    try:
        async with runtime.workflow.run(request) as runner:
            # Runner.__aenter__ restores the workflow's build context, so request
            # identifiers must be scoped after entering and before result().
            with Context.scope(
                conversation_id=session_id,
                user_id=request.user.id,
                workflow_trace_id=int(trace_id, 16) if trace_id else None,
                workflow_run_id=workflow_run_id,
            ):
                result = await runner.result()
        response = result if isinstance(result, Response) else Response.model_validate(result)
    except Exception as exc:
        if _is_content_filter(exc):
            log.warning("chat refused (content filter) session=%s", session_id)
            response = _refusal()
        else:
            log.exception("chat failed session=%s: %s", session_id, exc)
            raise
    _append_footer(response, runtime.footers)
    response.meta.latency_ms = round((time.perf_counter() - started) * 1000, 1)
    response.meta.trace_id = trace_id
    specialist = runtime.specialists.get(response.meta.route or "")
    response.meta.model = specialist.model if specialist else None
    if response.state == ResponseState.CONFIRM and response.pending_action is not None:
        await _pending_store().put(session_id, response.pending_action.model_dump_json())
    await session.add_items(
        [
            {"role": "user", "content": query},
            {"role": "assistant", "content": response.answer},
        ]
    )
    log.info(
        "chat done session=%s trace=%s route=%s state=%s ms=%s answer[%d]",
        session_id,
        trace_id or "off",
        response.meta.route,
        response.state.value,
        response.meta.latency_ms,
        len(response.answer),
    )
    return response


async def run_agent(
    query: str,
    session_id: str = "default",
    user: User | None = None,
) -> Response:
    return await _execute_turn(query, session_id, user)


_FINAL = "__final__"
_ERROR = "__error__"


async def run_agent_streamed(
    query: str,
    session_id: str = "default",
    user: User | None = None,
) -> AsyncIterator[dict]:
    queue: asyncio.Queue = asyncio.Queue()

    async def drive() -> None:
        token = set_sink(queue)
        try:
            response = await _execute_turn(query, session_id, user)
            queue.put_nowait({"type": _FINAL, "response": response})
        except Exception as exc:  # noqa: BLE001
            queue.put_nowait({"type": _ERROR, "message": str(exc)})
        finally:
            reset_sink(token)

    task = asyncio.create_task(drive())
    response: Response | None = None
    error: str | None = None
    try:
        while True:
            event = await queue.get()
            if event.get("type") == _FINAL:
                response = event["response"]
                break
            if event.get("type") == _ERROR:
                error = event["message"]
                break
            yield event
    finally:
        await task
    if error is not None:
        yield {"type": "error", "message": error}
        yield {"type": "done"}
        return
    assert response is not None
    meta_event = {
        "type": "meta",
        "route": response.meta.route,
        "handler_type": response.meta.handler_type,
    }
    if response.meta.model:
        meta_event["model"] = response.meta.model
    if response.meta.trace_id:
        meta_event["trace_id"] = response.meta.trace_id
    yield meta_event
    for index in range(0, len(response.answer), 48):
        yield {"type": "delta", "text": response.answer[index : index + 48]}
    if response.sources:
        yield {
            "type": "sources",
            "sources": [source.model_dump() for source in response.sources],
        }
    if response.related_questions:
        yield {
            "type": "related",
            "related_questions": response.related_questions,
        }
    yield {
        "type": "state",
        "state": response.state.value,
        "pending_action": (
            response.pending_action.model_dump() if response.pending_action else None
        ),
        "clarify_question": response.clarify_question,
    }
    yield {"type": "done"}


if __name__ == "__main__":
    import sys

    question = sys.argv[1] if len(sys.argv) > 1 else "What can you help me with?"
    answer = asyncio.run(run_agent(question, session_id="cli"))
    print("route:", answer.meta.route)
    print("answer:", answer.answer[:800])
    print("sources:", [(source.title, source.url) for source in answer.sources])
