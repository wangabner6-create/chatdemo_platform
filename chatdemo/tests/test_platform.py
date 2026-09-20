"""No-network test suite covering workflow logic, the skill registry, the safety
classifier, storage helpers, and how the orchestration runtime gets assembled.
Anything that hits a real provider needs its own endpoint and credentials set up."""

from __future__ import annotations

import pytest

from chatdemo.nemo_runtime import content_root
from chatdemo.registry import list_skills
from chatdemo.runner.mock import MockModelClient

ROOT = content_root()  # the bundled content directory, src/chatdemo/content


# --- registry / skills ------------------------------------------------------
def test_list_skills_finds_demo_assistant() -> None:
    names = [s.name for s in list_skills(ROOT)]
    assert "demo-assistant" in names


# --- workflow DAG engine -----------------------------------------------------
def test_when_predicate_is_safe_and_correct() -> None:
    from chatdemo.handlers.workflow import _safe_predicate

    assert _safe_predicate("'others' not in intent", {"intent": ["faq"]}) is True
    assert _safe_predicate("'others' not in intent", {"intent": ["others"]}) is False
    assert _safe_predicate("results", {"results": [1]}) is True
    assert _safe_predicate("not results", {"results": None}) is True
    assert _safe_predicate("len(results) > 0", {"results": []}) is False
    with pytest.raises(ValueError):
        _safe_predicate(
            "__import__('os').system('x')", {}
        )  # this AST node type isn't on the allowed list


def test_independent_steps_share_a_level() -> None:
    from chatdemo.contracts import Descriptor
    from chatdemo.handlers.base import HandlerDeps
    from chatdemo.handlers.workflow import StepSpec, WorkflowHandler
    from chatdemo.runner import build_runner
    from chatdemo.runner.base import ModelSettings

    async def _noop(inputs, ctx, deps):
        return None

    deps = HandlerDeps(runner=build_runner(ModelSettings(), MockModelClient()))
    desc = Descriptor(id="w", name="w", description="w", kind="workflow")
    # Since `answer` and `related` each depend on `results` but neither depends on the other,
    # the scheduler should place them at the same level.
    fetch = StepSpec(id="fetch", fn=_noop, out="results")
    answer = StepSpec(id="answer", fn=_noop, inputs={"r": "results"}, out="answer")
    related = StepSpec(id="related", fn=_noop, inputs={"r": "results"}, out="related")
    handler = WorkflowHandler(desc, [fetch, answer, related], {}, deps)
    parallel = next(
        ids for ids in ([s.id for s in lv] for lv in handler._levels) if "answer" in ids
    )
    assert {"answer", "related"} <= set(parallel)


def test_cycle_is_rejected() -> None:
    from chatdemo.contracts import Descriptor
    from chatdemo.handlers.base import HandlerDeps
    from chatdemo.handlers.workflow import StepSpec, WorkflowHandler
    from chatdemo.runner import build_runner
    from chatdemo.runner.base import ModelSettings

    async def _noop(inputs, ctx, deps):
        return None

    deps = HandlerDeps(runner=build_runner(ModelSettings(), MockModelClient()))
    desc = Descriptor(id="c", name="c", description="c", kind="workflow")
    a = StepSpec(id="a", fn=_noop, inputs={"x": "b"}, out="a")  # step a depends on b
    b = StepSpec(
        id="b", fn=_noop, inputs={"x": "a"}, out="b"
    )  # step b depends on a, forming a cycle
    with pytest.raises(ValueError):
        WorkflowHandler(desc, [a, b], {}, deps)


# --- safety classifier ------------------------------------------------------
def test_safety_parsing() -> None:
    from chatdemo.safety import LLMSafetyChecker

    assert LLMSafetyChecker._is_safe('{"category": ["safe"]}') is True
    assert LLMSafetyChecker._is_safe('{"category": ["toxic"]}') is False
    assert (
        LLMSafetyChecker._is_safe("not json") is True
    )  # unparseable output defaults to allowing the request


# --- settings / config -------------------------------------------------------
def test_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from chatdemo.config import Settings

    monkeypatch.setenv("CHATDEMO_API_STYLE", "response")
    monkeypatch.setenv("CHATDEMO_MODEL", "some-model")
    s = Settings.from_env()
    assert s.model_settings().api_style == "response"
    assert s.safety is True
    assert s.websearch_tool == "web_search"


def test_phoenix_tracing_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from chatdemo.config import Settings

    monkeypatch.setenv("CHATDEMO_TRACING", "on")
    monkeypatch.setenv("CHATDEMO_PHOENIX_ENDPOINT", "http://phoenix:6006/v1/traces")
    monkeypatch.setenv("CHATDEMO_TRACE_PROJECT", "chatdemo-ci")

    settings = Settings.from_env()
    assert settings.tracing is True
    assert settings.phoenix_endpoint == "http://phoenix:6006/v1/traces"
    assert settings.trace_project == "chatdemo-ci"


def test_trace_step_is_noop_outside_workflow() -> None:
    from chatdemo.observability import trace_step

    with trace_step("unit.noop", attributes={"test": True}) as span:
        span.set_tokens(3)
        span.set_output({"ok": True})


def test_completion_generation_params(monkeypatch: pytest.MonkeyPatch) -> None:
    from chatdemo.config import Settings

    monkeypatch.setenv("CHATDEMO_API_STYLE", "completion")
    monkeypatch.setenv("CHATDEMO_MAX_TOKENS", "512")
    monkeypatch.setenv("CHATDEMO_ENABLE_THINKING", "false")
    settings = Settings.from_env()

    expected = {
        "max_tokens": 512,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }
    assert settings.completion_params() == expected
    assert settings.model_settings().params == expected


def test_per_role_models_fall_back_to_main(monkeypatch: pytest.MonkeyPatch) -> None:
    from chatdemo.config import Settings

    # with just CHATDEMO_MODEL / CHATDEMO_API_STYLE set, every role should fall back to them
    for var in (
        "CHATDEMO_ROUTER_MODEL",
        "CHATDEMO_SKILL_MODEL",
        "CHATDEMO_WF_MODEL",
        "CHATDEMO_RELATED_MODEL",
        "CHATDEMO_ROUTER_API_STYLE",
        "CHATDEMO_SKILL_API_STYLE",
        "CHATDEMO_RELATED_API_STYLE",
        "CHATDEMO_WF_API_STYLE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CHATDEMO_MODEL", "main-m")
    monkeypatch.setenv("CHATDEMO_API_STYLE", "response")
    s = Settings.from_env()
    for m in (s.router_model, s.skill_model, s.wf_model, s.related_model):
        assert m == "main-m"
    for st in (s.router_api_style, s.skill_api_style, s.related_api_style):
        assert st == "response"
    assert (
        s.wf_api_style == ""
    )  # RAW: leaving this unset means each workflow keeps its own manifest api_style
    assert s.related_settings().model == "main-m" and s.related_settings().api_style == "response"


def test_per_role_models_override_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    from chatdemo.config import Settings

    monkeypatch.setenv("CHATDEMO_MODEL", "main-m")
    monkeypatch.setenv("CHATDEMO_API_STYLE", "completion")
    monkeypatch.setenv("CHATDEMO_SKILL_MODEL", "skill-m")
    monkeypatch.setenv("CHATDEMO_SKILL_API_STYLE", "response")
    monkeypatch.setenv("CHATDEMO_ROUTER_MODEL", "rtr-m")
    s = Settings.from_env()
    assert (s.skill_model, s.skill_api_style) == ("skill-m", "response")
    assert (s.router_model, s.router_api_style) == (
        "rtr-m",
        "completion",
    )  # api_style falls back to the main setting
    assert (s.wf_model, s.related_model) == (
        "main-m",
        "main-m",
    )  # roles with no override just use the main model


# --- SSE streaming ------------------------------------------------------------
def test_streaming_serialization() -> None:
    from chatdemo.contracts import Response, ResponseMeta
    from chatdemo.streaming import to_sse_events

    resp = Response(
        answer="Hello from the demo assistant.", meta=ResponseMeta(route="demo-assistant")
    )
    events = list(to_sse_events(resp))
    assert '"type": "meta"' in events[0]
    assert '"type": "done"' in events[-1]


# --- runtime assembly, exercised without any network access ------------------
@pytest.mark.asyncio
async def test_nemo_workflow_executes_registered_entry_function() -> None:
    from chatdemo.config import Settings
    from chatdemo.nemo_runtime import AgentRequest, build_runtime

    rt = await build_runtime(ROOT, Settings.from_env())
    try:
        assert rt.framework == "nvidia-nat"
        assert rt.framework_version == "1.8.0"
        async with rt.workflow.run(AgentRequest(query="native probe", session_id="nemo")) as runner:
            response = await runner.result()
        assert response.answer == "[mock answer] native probe"
        assert response.meta.route == "mock"
    finally:
        await rt.close()


def test_append_footer_is_deterministic_and_idempotent() -> None:
    from chatdemo.agents_runtime import _append_footer
    from chatdemo.contracts import Response

    footers = {"demo-assistant": "Skill source: https://example.com/demo-assistant"}

    resp = Response(answer="Body.")
    resp.meta.route = "demo-assistant"
    suffix = _append_footer(resp, footers)
    assert suffix.startswith("\n\n") and "demo-assistant" in suffix
    assert resp.answer == "Body." + suffix
    assert (
        _append_footer(resp, footers) == ""
    )  # calling it again is a no-op since the footer is already appended
    assert resp.answer.count("Skill source") == 1

    for route in ("general-fallback", None):
        response = Response(answer="x")
        response.meta.route = route
        assert _append_footer(response, footers) == ""
        assert response.answer == "x"


@pytest.mark.asyncio
async def test_fallback_scope_is_grounded_in_enabled_specialists() -> None:
    # When general-fallback answers a "what can you do" question, it needs to reflect
    # the CURRENTLY enabled specialists (workflows + skills) rather than some static, manually kept list.
    from chatdemo.agents_runtime import FALLBACK_NAME, build_runtime
    from chatdemo.config import Settings

    rt = await build_runtime(ROOT, Settings.from_env())
    try:
        instr = rt.specialists[FALLBACK_NAME].instructions
        assert "## Current scope" in instr
        assert "demo-assistant:" in instr
        assert f"\n- {FALLBACK_NAME}:" not in instr
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_model_tiers_by_search_need() -> None:
    from chatdemo.agents_runtime import build_runtime
    from chatdemo.config import Settings

    rt = await build_runtime(ROOT, Settings.from_env())
    try:
        assert rt.triage.api_style == "completion"
        demo = rt.specialists["demo-assistant"]
        assert demo.api_style == "response"
        assert demo.web_search is True
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_skill_tools_are_scoped_orchestration_functions() -> None:
    from chatdemo.agents_runtime import build_runtime
    from chatdemo.config import Settings

    rt = await build_runtime(ROOT, Settings.from_env())
    try:
        specialist = rt.specialists["demo-assistant"]
        assert specialist.kind == "skill"
        assert set(specialist.tools) == {"submit_feedback"}
        assert specialist.tools["submit_feedback"].spec.side_effecting is True
        assert specialist.tools["submit_feedback"].function_name.startswith(
            "demo_assistant__tool__"
        )
        assert "submit_feedback" not in rt.specialists["general-fallback"].tools
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_chat_safe_session_drops_responses_only_items() -> None:
    from chatdemo.store import ChatSafeSession

    class _Mem:
        session_id = "t"
        session_settings = None

        def __init__(self):
            self.items = []

        async def get_items(self, limit=None):
            return self.items[-limit:] if limit else list(self.items)

        async def add_items(self, items):
            self.items.extend(items)

        async def pop_item(self):
            return self.items.pop() if self.items else None

        async def clear_session(self):
            self.items = []

    inner = _Mem()
    s = ChatSafeSession(inner)
    await s.add_items(
        [
            {"role": "user", "content": "hi"},
            {"type": "web_search_call", "action": {"type": "search"}, "status": "completed"},
            {"type": "reasoning", "summary": []},
            {
                "type": "some_future_hosted_call",
                "foo": 1,
            },  # not a recognized type -> automatically discarded
            # A reasoning-capable Responses model produces a function_call alongside a
            # matching reasoning item. Chat Completions has no way to consume the reasoning
            # item, so it gets dropped — but that means its paired function_call has to be
            # dropped too, otherwise a subsequent Responses reasoning turn will 400 because
            # of the now-orphaned function_call.
            {
                "type": "function_call",
                "name": "submit_feedback",
                "call_id": "c1",
                "arguments": "{}",
            },
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hi"}],
            },
        ]
    )
    kept = await s.get_items()
    keys = [it.get("type") or it.get("role") for it in kept]
    # only plain conversation messages survive the allowlist; every tool-call artifact is stripped out
    assert keys == ["user", "assistant"]
    # surviving messages get their content collapsed down to a plain string so the
    # Chat Completions converter can handle them (a Responses `output_text` part becomes "hi")
    assistant = next(it for it in kept if it.get("role") == "assistant")
    assert assistant["content"] == "hi" and isinstance(assistant["content"], str)


# --- sandboxed script execution, deterministic behavior -----------------------
def test_run_skill_script_allowlist_env_timeout(tmp_path, monkeypatch) -> None:
    from pathlib import Path

    from chatdemo.skill_scripts import run_skill_script

    monkeypatch.setenv("CHATDEMO_API_KEY", "SECRET-should-not-leak")
    d = tmp_path / "scripts"
    d.mkdir()
    (d / "echo.py").write_text(
        "import os,sys\nprint('args=' + ','.join(sys.argv[1:]))\n"
        "print('key=' + os.environ.get('CHATDEMO_API_KEY', 'MISSING'))\n"
    )
    scripts = {p.name: p for p in d.glob("*")}

    out = run_skill_script(scripts, "echo.py", ["a", "b"], timeout=10)
    assert "args=a,b" in out  # confirms the arguments reached the script
    assert "key=MISSING" in out  # the script ran with a sanitized env, so the secret wasn't exposed
    assert "SECRET-should-not-leak" not in out
    # names outside the allowlist, including path-traversal attempts, are refused before execution
    assert "not allowed" in run_skill_script(scripts, "../evil.py", [], 10)
    assert "unsupported" in run_skill_script({"x.bin": Path("/x.bin")}, "x.bin", [], 10)


# --- feedback capture ----------------------------------------------------------
def test_submit_feedback_tool_has_vote() -> None:
    from chatdemo.tools_loading import load_tools

    sf = load_tools(ROOT, ROOT, ["submit_feedback"])["submit_feedback"]
    props = sf.spec.input_schema["properties"]
    assert set(props["vote"]["enum"]) == {"up", "down"}
    assert "vote" not in sf.spec.input_schema["required"]  # not a required field


def test_feedback_store_memory_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # When Redis isn't configured, records fall back to an in-memory list for local dev.
    monkeypatch.delenv("CHATDEMO_REDIS_URL", raising=False)
    from chatdemo import feedback_store as fs

    fs._MEM.clear()
    rec = fs.record_feedback(
        {
            "input_query": "q",
            "response": "a",
            "feedback": "down",
            "source": "thumb",
            "username": "u",
        }
    )
    assert rec["id"] and rec["timestamp"]  # auto-populated by the store
    assert rec["feedback"] == "down"
    recent = fs.recent_feedback(5)
    assert recent and recent[0]["source"] == "thumb"


def test_submit_feedback_handler_writes_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHATDEMO_REDIS_URL", raising=False)
    from types import SimpleNamespace

    from chatdemo import feedback_store as fs
    from chatdemo.tools_loading import load_tools

    fs._MEM.clear()
    sf = load_tools(ROOT, ROOT, ["submit_feedback"])["submit_feedback"]
    out = sf.executor(
        {"summary": "nice", "category": "other", "vote": "up"},
        SimpleNamespace(id="user-mei", session_id="sess1"),
    )
    assert out["ticket_id"] and out["vote"] == "up" and out["submitted_by"] == "user-mei"
    assert fs._MEM[-1]["feedback"] == "up" and fs._MEM[-1]["source"] == "chat"


def test_feedback_admin_endpoint_token_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # GET /feedback exposes a read-only admin view. It stays disabled until
    # CHATDEMO_ADMIN_TOKEN is configured, at which point a matching X-Admin-Token header is required.
    from fastapi.testclient import TestClient

    from chatdemo import feedback_store as fs
    from chatdemo.api import create_app

    monkeypatch.delenv("CHATDEMO_REDIS_URL", raising=False)
    fs._MEM.clear()
    fs.record_feedback({"comment": "hi", "source": "sidebar", "username": "user-mei"})
    fs.record_feedback({"feedback": "up", "source": "thumb"})

    # Without a configured token, the endpoint stays disabled (403) regardless of any header sent.
    monkeypatch.delenv("CHATDEMO_ADMIN_TOKEN", raising=False)
    client = TestClient(create_app())
    assert client.get("/feedback", headers={"X-Admin-Token": "x"}).status_code == 403

    # Once a token is configured: absent or incorrect header -> 401; the right one -> 200 with every record.
    monkeypatch.setenv("CHATDEMO_ADMIN_TOKEN", "s3cret")
    assert client.get("/feedback").status_code == 401
    assert client.get("/feedback", headers={"X-Admin-Token": "nope"}).status_code == 401
    ok = client.get("/feedback", headers={"X-Admin-Token": "s3cret"})
    assert ok.status_code == 200 and ok.json()["count"] == 2
    # the limit param is optional; when given, it caps how many records come back, newest first.
    one = client.get("/feedback?limit=1", headers={"X-Admin-Token": "s3cret"})
    assert one.json()["count"] == 1


def test_feedback_links_to_trace_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from chatdemo import feedback_store as fs
    from chatdemo.api import create_app

    monkeypatch.delenv("CHATDEMO_REDIS_URL", raising=False)
    fs._MEM.clear()
    client = TestClient(create_app())

    response = client.post(
        "/feedback",
        json={
            "session_id": "sess-traced",
            "question": "How is state stored?",
            "answer": "In ChatSafeSession.",
            "vote": "up",
            "source": "thumb",
            "trace_id": "2bb8a3741a6947249b3cc510326049ce",
            "route": "docs-assistant",
            "model": "qwen3:4b-instruct",
        },
    )

    assert response.status_code == 200
    assert fs._MEM[-1]["trace_id"] == "2bb8a3741a6947249b3cc510326049ce"
    assert fs._MEM[-1]["route"] == "docs-assistant"
    assert fs._MEM[-1]["model"] == "qwen3:4b-instruct"


def test_pending_action_is_provider_neutral_json() -> None:
    from chatdemo.contracts import PendingAction

    pending = PendingAction(
        tool_name="submit_feedback",
        arguments={"summary": "Great demo", "category": "other"},
        handler_id="demo-assistant",
    )
    restored = PendingAction.model_validate_json(pending.model_dump_json())
    assert restored == pending


@pytest.mark.asyncio
async def test_mock_agent_turn_stays_offline(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from chatdemo.agents_runtime import run_agent

    monkeypatch.setenv("CHATDEMO_BASE_URL", "")
    monkeypatch.setenv("CHATDEMO_API_KEY", "")
    monkeypatch.setenv("CHATDEMO_MODEL", "mock")
    monkeypatch.setenv("CHATDEMO_REDIS_URL", "")
    monkeypatch.setenv("CHATDEMO_AGENTS_DB", str(tmp_path / "mock-agent.db"))

    response = await run_agent("hello offline", session_id="mock-agent")

    assert response.answer == "[mock answer] hello offline"
    assert response.meta.route == "mock"
    assert response.meta.handler_type == "mock"


@pytest.mark.asyncio
async def test_mock_agent_stream_stays_offline(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from chatdemo.agents_runtime import run_agent_streamed

    monkeypatch.setenv("CHATDEMO_BASE_URL", "")
    monkeypatch.setenv("CHATDEMO_API_KEY", "")
    monkeypatch.setenv("CHATDEMO_MODEL", "mock")
    monkeypatch.setenv("CHATDEMO_REDIS_URL", "")
    monkeypatch.setenv("CHATDEMO_AGENTS_DB", str(tmp_path / "mock-stream.db"))

    events = [
        event async for event in run_agent_streamed("stream offline", session_id="mock-stream")
    ]

    assert events[0] == {"type": "meta", "route": "mock", "handler_type": "mock"}
    assert "".join(event["text"] for event in events if event["type"] == "delta") == (
        "[mock answer] stream offline"
    )
    assert events[-1] == {"type": "done"}


@pytest.mark.asyncio
async def test_enabled_allowlist_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    from chatdemo.agents_runtime import build_runtime
    from chatdemo.config import Settings

    monkeypatch.setenv("CHATDEMO_ENABLED", "demo-assistant")  # restrict to just demo-assistant
    rt = await build_runtime(ROOT, Settings.from_env())
    try:
        assert set(rt.specialists) == {"demo-assistant"}
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_fixed_route_must_be_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from chatdemo.agents_runtime import build_runtime
    from chatdemo.config import Settings

    monkeypatch.setenv("CHATDEMO_ENABLED", "demo-assistant")
    monkeypatch.setenv("CHATDEMO_ROUTE", "general-fallback")  # this route isn't in the enabled set
    with pytest.raises(ValueError):
        await build_runtime(ROOT, Settings.from_env())


def test_context_sources_fill_response() -> None:
    from chatdemo.agents_runtime import ChatContext, _with_context_sources
    from chatdemo.contracts import Response, Source

    cx = ChatContext(
        user={"id": "u"}, session_id="s", history=[], sources=[{"title": "T", "url": "http://x"}]
    )
    # when the answer has no sources of its own, they get backfilled from the per-turn collector
    assert [s.url for s in _with_context_sources(Response(answer="a"), cx).sources] == ["http://x"]
    # if the answer already carries sources (say, from web_search), leave them as-is
    r2 = Response(answer="a", sources=[Source(title="W", url="http://w")])
    assert _with_context_sources(r2, cx).sources[0].url == "http://w"
    # with no context available (or nothing was collected), this is effectively a no-op
    assert _with_context_sources(Response(answer="a"), None).sources == []


def test_tool_user_exposes_sources_sink() -> None:
    from types import SimpleNamespace

    from chatdemo.agents_runtime import ChatContext, _tool_user

    cx = ChatContext(user={"id": "u", "name": "Mei"}, session_id="s", history=[], sources=[])
    u = _tool_user(SimpleNamespace(context=cx))
    assert (
        u.sources is cx.sources
    )  # identical list reference, so anything a tool appends shows up here too
    u.sources.append({"url": "http://x"})
    assert cx.sources == [{"url": "http://x"}]
    # on the resume path, where there's no context, the sink comes back None and the handler checks for that
    assert _tool_user(SimpleNamespace(context=None)).sources is None
