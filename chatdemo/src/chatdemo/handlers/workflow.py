"""WorkflowHandler: executes a DAG of steps defined directly in code (see designs D2/D16).

A step spec lists what it consumes (`in`), the channel it produces (`out`), an
optional run condition (`when`), any manual ordering constraints (`after`), and
how many times to retry on failure. The engine infers the dependency graph from
which channels each step reads, which lets unrelated steps execute in parallel
via a level-by-level topological ordering. State moves through a shared
blackboard dict mapping channel name to value, alongside two read-only views:
`input` (the current turn) and `ctx` (user, session, and history). Note that
approval/confirmation for action tools lives in the Agents layer, not here —
everything in this engine is deterministic computation with no side-effect
gating of its own.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..contracts import Descriptor, RequestContext, Response, ResponseMeta, ResponseState, Source
from ..observability import emit, log
from .base import HandlerDeps

# signature: takes the already-resolved input dict, the request context, and shared deps; yields the step's result
StepRun = Callable[[dict, RequestContext, HandlerDeps], Awaitable[Any]]


@dataclass
class StepSpec:
    id: str
    fn: StepRun
    inputs: dict[str, str] = field(default_factory=dict)  # maps a param name to the expression that supplies it
    out: str | None = None                                # name of the channel this step publishes to
    when: str | None = None                               # condition that must hold for the step to run
    after: list[str] = field(default_factory=list)        # ids of steps this one must wait on regardless of data deps
    retry: int = 0


# --------------------------------------------------------------------------- #
# `when:` conditions are evaluated through a restricted AST walker, not eval() — keeps arbitrary code out of the picture
# --------------------------------------------------------------------------- #
_ALLOWED = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.Gt, ast.LtE, ast.GtE,
    ast.In, ast.NotIn, ast.Name, ast.Load, ast.Constant, ast.List, ast.Tuple, ast.Call,
)


def _cmp(op: ast.cmpop, left: Any, right: Any) -> bool:
    if isinstance(op, ast.Eq):
        return left == right
    if isinstance(op, ast.NotEq):
        return left != right
    if isinstance(op, ast.Lt):
        return left < right
    if isinstance(op, ast.Gt):
        return left > right
    if isinstance(op, ast.LtE):
        return left <= right
    if isinstance(op, ast.GtE):
        return left >= right
    if isinstance(op, ast.In):
        return left in (right or [])
    if isinstance(op, ast.NotIn):
        return left not in (right or [])
    raise ValueError(f"unsupported comparison {type(op).__name__}")


def _eval(node: ast.AST, ns: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, ns)
    if isinstance(node, ast.BoolOp):
        vals = [_eval(v, ns) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval(node.operand, ns)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, ns)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, ns)
            if not _cmp(op, left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Name):
        return ns.get(node.id)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e, ns) for e in node.elts]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len":
        return len(_eval(node.args[0], ns) or [])
    raise ValueError(f"disallowed expression node: {type(node).__name__}")


def _safe_predicate(expr: str, ns: dict[str, Any]) -> bool:
    tree = ast.parse(expr, mode="eval")
    for n in ast.walk(tree):
        if not isinstance(n, _ALLOWED):
            raise ValueError(f"unsafe `when` predicate {expr!r}: {type(n).__name__}")
    return bool(_eval(tree, ns))


def _names(expr: str) -> set[str]:
    return {n.id for n in ast.walk(ast.parse(expr, mode="eval")) if isinstance(n, ast.Name)}


# --------------------------------------------------------------------------- #
# Resolves an input expression of the form input.* / ctx.* / <channel>[.field...]
# --------------------------------------------------------------------------- #
def _path(obj: Any, parts: list[str]) -> Any:
    for p in parts:
        if obj is None:
            return None
        obj = obj.get(p) if isinstance(obj, dict) else getattr(obj, p, None)
    return obj


def _resolve(expr: str, state: dict[str, Any], ctx: RequestContext) -> Any:
    head, *rest = expr.split(".")
    if head == "input":
        return _path({"query": ctx.query, **(ctx.product_config or {})}, rest)
    if head == "ctx":
        base = {
            "history": ctx.session.history,
            "user": ctx.user,
            "session": ctx.session,
            "previous_route": ctx.session.previous_route,
        }
        return _path(base, rest)
    return _path(state.get(head), rest)  # falls through to a channel value written by an earlier step


def _preview(value: Any) -> str:
    """Condenses a step's output into a short string safe to write at INFO level.
    Source objects render with title and url together so what got retrieved is
    obvious without digging further."""
    if value is None:
        return "None"
    if isinstance(value, str):
        return value[:200]
    if isinstance(value, Source):
        return f"{(value.title or '')[:60]} — {value.url or ''}"
    if isinstance(value, (list, tuple)):
        if value and isinstance(value[0], Source):
            shown = "; ".join(
                f"[{i}] {(s.title or '')[:50]} — {s.url or ''}" for i, s in enumerate(value[:8], 1)
            )
            extra = f" (+{len(value) - 8} more)" if len(value) > 8 else ""
            return f"{len(value)} sources: {shown}{extra}"
        return f"{type(value).__name__}[{len(value)}]"
    if isinstance(value, dict):
        return f"dict{list(value)[:8]}"
    if isinstance(value, Response):
        return f"Response(state={value.state.value}, answer[{len(value.answer)}])"
    return type(value).__name__


def _full(value: Any) -> str:
    """Unabridged, human-readable rendering of a step's output, used for DEBUG-level
    logging (source entries include their snippet text; strings are printed in
    their entirety rather than truncated)."""
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], Source):
        return "\n".join(
            f"  [{i}] {s.title or '(no title)'} | {s.url or ''}\n      {(s.snippet or '')[:600]}"
            for i, s in enumerate(value, 1)
        )
    if isinstance(value, str):
        return value
    return repr(value)


class WorkflowHandler:
    def __init__(
        self,
        descriptor: Descriptor,
        steps: list[StepSpec],
        output_map: dict[str, str],
        deps: HandlerDeps,
    ) -> None:
        self._descriptor = descriptor
        self._order = steps
        self._steps = {s.id: s for s in steps}
        self._output = output_map
        self._deps = deps
        deps.handler_id = descriptor.id
        self._producers: dict[str, list[str]] = {}
        for s in steps:
            if s.out:
                self._producers.setdefault(s.out, []).append(s.id)
        self._levels = self._compute_levels()  # constructing this also catches a malformed DAG immediately

    @property
    def descriptor(self) -> Descriptor:
        return self._descriptor

    @property
    def action_tools(self) -> dict:
        """The set of strongly-typed tools this workflow exposes, used to register it under its owner's scope in orchestration."""
        return self._deps.tools

    @property
    def run_model_settings(self):
        """Exposes the (model, api_style) pair actually used by this workflow's step
        runner, letting the run_workflow shell agent mirror the workflow's own
        configuration instead of falling back to the router/API-style defaults."""
        return self._deps.runner.settings

    # -- graph scheduling ----------------------------------------------------- #
    def _deps_of(self, s: StepSpec) -> set[str]:
        refs: set[str] = set()
        for src in s.inputs.values():
            head = src.split(".")[0]
            if head not in ("input", "ctx"):
                refs.add(head)
        if s.when:
            refs |= _names(s.when)
        out: set[str] = set(s.after)
        for ch in refs:
            for producer in self._producers.get(ch, []):
                if producer != s.id:
                    out.add(producer)
        return out

    def _compute_levels(self) -> list[list[StepSpec]]:
        dep = {s.id: self._deps_of(s) for s in self._order}
        completed: set[str] = set()
        remaining = [s.id for s in self._order]
        levels: list[list[StepSpec]] = []
        while remaining:
            ready = [sid for sid in remaining if dep[sid] <= completed]
            if not ready:
                raise ValueError(
                    f"workflow {self._descriptor.id}: cycle or missing dependency among {remaining}"
                )
            levels.append([self._steps[sid] for sid in ready])
            completed.update(ready)
            remaining = [sid for sid in remaining if sid not in completed]
        return levels

    # -- execution ---------------------------------------------------------- #
    async def execute(self, ctx: RequestContext) -> Response:
        wid = self._descriptor.id
        emit(
            "workflow",
            event="start",
            id=wid,
            query=ctx.query,
            levels=len(self._levels),
            steps=len(self._order),
        )
        started = time.perf_counter()
        state: dict[str, Any] = {}
        for i, level in enumerate(self._levels):
            emit("level", id=wid, index=i, steps=[s.id for s in level])
            await asyncio.gather(*(self._run_step(s, state, ctx) for s in level))
        ms = round((time.perf_counter() - started) * 1000, 1)
        emit("workflow", event="end", id=wid, ms=ms, channels=sorted(state))
        return self._assemble(state, ctx)

    async def _run_step(self, s: StepSpec, state: dict[str, Any], ctx: RequestContext) -> None:
        if s.when is not None:
            ns = {ch: state.get(ch) for ch in self._producers}
            if not _safe_predicate(s.when, ns):
                emit("step", event="skip", id=s.id, when=s.when)
                return  # skipped: its output channel simply stays unset (None)
        inputs = {k: _resolve(v, state, ctx) for k, v in s.inputs.items()}
        emit("step", event="start", id=s.id, out=s.out, inputs=sorted(inputs))
        started = time.perf_counter()
        attempt = 0
        while True:
            try:
                value = await s.fn(inputs, ctx, self._deps)
                break
            except Exception as exc:  # noqa: BLE001 — broad catch is intentional, it's what drives the per-step retry logic
                if attempt >= s.retry:
                    emit("step", event="error", id=s.id, attempt=attempt, error=repr(exc))
                    raise
                attempt += 1
                emit("step", event="retry", id=s.id, attempt=attempt, error=repr(exc))
        ms = round((time.perf_counter() - started) * 1000, 1)
        emit("step", event="end", id=s.id, out=s.out, ms=ms, result=_preview(value))
        # emitted only when DEBUG logging is on (set CHATDEMO_LOG_LEVEL=DEBUG) — includes source snippets and untruncated text
        if log.isEnabledFor(logging.DEBUG):
            log.debug("step %s -> %s =\n%s", s.id, s.out, _full(value))
        if s.out is not None:
            state[s.out] = value

    def _assemble(self, state: dict[str, Any], ctx: RequestContext) -> Response:
        answer = state.get(self._output.get("answer", ""))
        if isinstance(answer, Response):  # some steps (e.g. a clarify step) hand back a complete Response object
            answer.meta = self._meta()
            return answer
        sources = state.get(self._output.get("sources", "")) or []
        related = state.get(self._output.get("related_questions", "")) or []
        return Response(
            answer=answer if isinstance(answer, str) else ("" if answer is None else str(answer)),
            sources=[x if isinstance(x, Source) else Source(**x) for x in sources],
            related_questions=list(related),
            state=ResponseState.ANSWER,
            meta=self._meta(),
        )

    def _meta(self) -> ResponseMeta:
        return ResponseMeta(route=self._descriptor.id, handler_type="workflow")
