# Worked example: a minimal `docs-qa` workflow

A complete, copy-paste workflow that shows every concept: sequential steps, a
parallel level, a conditional branch, a retriever with offline fallback, and the
output map. Create it at `chatdemo/src/chatdemo/content/workflows/docs-qa/`.

## DAG shape

```
   L0  rewrite                         (fold multi-turn history into a standalone query)
        │ query
   L1  retrieve                        (deps.retriever.search -> results)
        │ results
   L2  answer  ∥  related  ∥  no_results
       when     when          when
       results  results       not results
```
`answer` and `no_results` both write the `answer_text` channel with mutually
exclusive `when` (branch). `answer` and `related` have no edge between them (parallel).

## `workflow.yaml`

```yaml
name: docs-qa
description: >
  Answers product-documentation questions using the internal docs search index.
  Use for "how do I", "what is", API/config/usage questions about the product.
runner:
  model: mock
  api_style: completion
retriever:
  type: http
  endpoint: ${DOCS_SEARCH}          # host:port; unset -> fixture
  k: 8
  fixture: fixtures/docs.json
steps:
  - id: rewrite
    in: {query: input.query, history: ctx.history}
    out: query
  - id: retrieve
    in: {query: query}
    out: results
  - id: answer
    in: {query: query, results: results}
    out: answer_text
    when: "results"
  - id: related
    in: {query: query, results: results}
    out: related
    when: "results"
  - id: no_results
    in: {query: query}
    out: answer_text
    when: "not results"
output:
  answer: answer_text
  related_questions: related
  sources: results
```

## `steps/rewrite.py`

```python
from __future__ import annotations
from chatdemo.contracts import Message, RequestContext, Role
from chatdemo.handlers.base import HandlerDeps

async def run(inputs: dict, ctx: RequestContext, deps: HandlerDeps) -> str:
    query = inputs["query"]
    history = inputs.get("history") or []
    if not history:                       # first turn -> nothing to fold in
        return query
    convo = "\n".join(f"{m.role.value}: {m.content}" for m in history[-6:])
    messages = [
        Message(role=Role.SYSTEM, content="Rewrite the latest question into a standalone query."),
        Message(role=Role.USER, content=f"Conversation:\n{convo}\n\nLatest: {query}"),
    ]
    result = await deps.runner.run(messages)
    return (result.text or query).strip()
```

## `steps/retrieve.py`

```python
from __future__ import annotations
from chatdemo.contracts import RequestContext, Source
from chatdemo.handlers.base import HandlerDeps

async def run(inputs: dict, ctx: RequestContext, deps: HandlerDeps) -> list[Source]:
    return list(await deps.retriever.search(inputs["query"], {}))
```

> This minimal example answers straight off `results`. A real retriever that
> returns document chunks should add a `process_sources` step between `retrieve`
> and `answer` (dedup by URL + number for `[n]` citations) and read the processed
> `sources` channel downstream — see "Post-processing retrieved sources" in
> `manifest-and-dag.md`.

## `steps/answer.py`

```python
from __future__ import annotations
from chatdemo.contracts import Message, RequestContext, Role
from chatdemo.handlers.base import HandlerDeps

async def run(inputs: dict, ctx: RequestContext, deps: HandlerDeps) -> str:
    results = inputs.get("results") or []
    context = "\n\n".join(s.snippet or s.title or "" for s in results if (s.snippet or s.title))
    messages = [
        Message(role=Role.SYSTEM, content=f"Answer using ONLY this context:\n{context}"),
        Message(role=Role.USER, content=inputs["query"]),
    ]
    result = await deps.runner.run(messages)
    return result.text
```

## `steps/related.py`  (runs in parallel with answer)

```python
from __future__ import annotations
import json
from chatdemo.contracts import Message, RequestContext, Role
from chatdemo.handlers.base import HandlerDeps

_SCHEMA = {"type": "json_schema", "schema": {
    "type": "object",
    "properties": {"questions": {"type": "array", "items": {"type": "string"}}},
    "required": ["questions"], "additionalProperties": False}}

async def run(inputs: dict, ctx: RequestContext, deps: HandlerDeps) -> list[str]:
    runner = deps.related_runner or deps.runner
    titles = "\n".join(s.title or "" for s in (inputs.get("results") or []))
    prompt = ('Given the question and retrieved titles, propose up to 3 follow-up '
              f'questions. Return JSON {{"questions": [...]}}.\n\nQ: {inputs["query"]}\n{titles}')
    result = await runner.run([Message(role=Role.USER, content=prompt)], response_format=_SCHEMA)
    try:
        return json.loads(result.text).get("questions", [])
    except json.JSONDecodeError:
        return []
```

## `steps/no_results.py`  (the `when: not results` branch)

```python
from __future__ import annotations
from chatdemo.contracts import RequestContext
from chatdemo.handlers.base import HandlerDeps

async def run(inputs: dict, ctx: RequestContext, deps: HandlerDeps) -> str:
    return ("I couldn't find that in the docs. Try rephrasing, or include the "
            "product area / feature name.")
```

## `retriever.py`

```python
from __future__ import annotations
import json, os
from pathlib import Path
from typing import Any
import httpx
from chatdemo.contracts import Source
from chatdemo.handlers.base import NullRetriever, Retriever

def build(cfg: dict[str, Any] | None, folder: Path) -> Retriever:
    cfg = cfg or {}
    ep = (cfg.get("endpoint") or "").strip()
    if ep.startswith("${") and ep.endswith("}"):
        ep = os.environ.get(ep[2:-1], "").strip()
    if not ep:                                   # offline -> fixture
        fx = cfg.get("fixture")
        return _Fixture(folder / fx) if fx else NullRetriever()
    if not ep.startswith(("http://", "https://")):
        ep = "http://" + ep
    return _HTTP(ep.rstrip("/"), int(cfg.get("k", 8)))

class _Fixture:
    def __init__(self, path): self._items = [Source(**x) for x in json.loads(Path(path).read_text())] if Path(path).exists() else []
    async def search(self, query, filters):
        terms = query.lower().split()
        hit = [s for s in self._items if any(t in f"{s.title or ''} {s.snippet or ''}".lower() for t in terms)]
        return (hit or self._items)[:5]

class _HTTP:
    def __init__(self, base, k): self._base, self._k = base, k
    async def search(self, query, filters):
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                r = await c.post(f"{self._base}/search", json={"query": query, "k": self._k})
                r.raise_for_status(); data = r.json()
        except (httpx.HTTPError, ValueError):
            return []
        return [Source(title=x.get("title"), url=x.get("url"), snippet=x.get("text"))
                for x in data.get("results", []) if isinstance(x, dict)]
```

## `fixtures/docs.json`

```json
[
  {"title": "Getting started", "url": "https://docs/start", "snippet": "Install with pip and configure the client."},
  {"title": "Configuration reference", "url": "https://docs/config", "snippet": "All settings are environment variables."}
]
```

## Verify

```
uv run pytest -q
uv run ruff check .
# smoke the DAG offline (mock model + fixture):
uv run python -c "import asyncio; from chatdemo.registry import Registry; \
from chatdemo.runner.mock import MockModelClient; from chatdemo.agents_runtime import content_root; \
from chatdemo.contracts import RequestContext, SessionState, User; \
h = Registry(content_root(), MockModelClient()).discover()['docs-qa']; \
print(asyncio.run(h.execute(RequestContext(query='how do I configure it?', \
session=SessionState(session_id='t'), user=User(id='u')))).model_dump())"
```

Expect a `Response` with `meta.handler_type == 'workflow'`, an `answer`, `sources`
from the fixture, and `related_questions`. Then it is automatically routable —
triage will hand off to `docs-qa` based on its `description`.
