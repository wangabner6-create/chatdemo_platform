# Manifest schema, DAG semantics, and the deps API

## `workflow.yaml` schema

```yaml
name: my-workflow          # REQUIRED. Unique id; also the folder name convention.
description: >             # REQUIRED. The router card — triage picks the workflow by this.
  One or two sentences describing exactly what questions this workflow answers.

runner:                    # OPTIONAL. The model channel for the workflow's own steps.
  model: mock              #   see "Runner model resolution" below
  api_style: completion    #   completion | response  (response also allows hosted tools)

retriever:                 # OPTIONAL. Passed verbatim as `cfg` to retriever.py::build(cfg, folder).
  type: http               #   your retriever.py decides what keys mean; these are examples
  endpoint: ${MY_ROUTER}   #   ${VAR} is env-expanded by your retriever.py
  k: 12
  fixture: fixtures/x.json  #  offline fallback

steps:                     # REQUIRED. The DAG nodes, in a valid topological order.
  - id: rewrite            #   REQUIRED unique step id
    fn: rewrite            #   OPTIONAL python file steps/<fn>.py (defaults to id)
    in:                    #   OPTIONAL param -> source; resolved and passed as `inputs`
      query: input.query
      history: ctx.history
    out: query             #   OPTIONAL channel this step's return value is written to
    when: "history"        #   OPTIONAL gate predicate (safe subset); false -> step skipped
    after: [other_step]    #   OPTIONAL explicit extra dependency (rarely needed)
    retry: 0               #   OPTIONAL re-run count on exception (default 0)

output:                    # OPTIONAL. Channel -> Response field.
  answer: response         #   Response.answer      <- state["response"]
  related_questions: related
  sources: results

tools:                     # OPTIONAL. Shared/local tool names -> owner-scoped orchestration functions.
  - submit_feedback        #   side effects require confirmation; NEVER called from a step
```

## Runner model resolution (how to give a workflow its own model)

`runner.model` decides which model this workflow's steps use. It is resolved by
`Registry._resolve_model` against `CHATDEMO_MODEL` (the deployed model):

| `runner.model` in the manifest | resolves to |
| --- | --- |
| `mock` | `CHATDEMO_MODEL` (or the mock client when no endpoint is set) |
| `${MY_VAR}` (env placeholder) | the value of env `MY_VAR`; **unset -> falls back to `CHATDEMO_MODEL`** |
| `some/literal-model-id` | that id, verbatim (ignores env) |

So there are three ways to control a workflow's step model:
1. **Follow the deployment** (default): `model: mock` -> uses `CHATDEMO_MODEL`.
2. **Its own env** (recommended when a workflow needs a different/cheaper model than
   the search skills): pick any env name and reference it —
   ```yaml
   runner:
     model: ${DOCS_QA_MODEL}   # set DOCS_QA_MODEL=cheap/mini; unset -> CHATDEMO_MODEL
     api_style: completion
   ```
   Remember to pass that env into the container (add it to docker-compose `backend`
   `environment:` and `.env.example`). The fallback means it's safe to leave unset.
3. **Pin a specific model**: `model: some/model-id` (hardcoded, no env).

The same `${VAR}`-with-fallback pattern is used for the retriever `endpoint`. This
is separate from the routing/skill model roles, which are driven by
`CHATDEMO_MODEL` (search skills, Responses) and `CHATDEMO_ROUTER_MODEL` (routing/action,
cheap Chat Completions).

## How the DAG is built (engine internals, for debugging)

At load time `WorkflowHandler.__init__`:
1. **Producers map**: `channel -> [step ids that declare it as `out`]`. A channel may
   have several producers (mutually exclusive branches writing the same channel).
2. **Dependencies** of a step = the producers of every channel it references in `in`
   (excluding `input.*` / `ctx.*`) **plus** channel names used in `when` **plus**
   explicit `after`.
3. **Levels** via level-based topological sort: repeatedly take all remaining steps
   whose dependencies are already completed — that set is one level and runs
   concurrently (`asyncio.gather`). If no step is ready but some remain, it raises
   `cycle or missing dependency`.

At run time `execute(ctx)` walks the levels; for each step it evaluates `when`
(skip if false), resolves `in` to an `inputs` dict, runs `fn` (with `retry`), and
writes the return value to `out`. Finally it maps channels to a `Response` via
`output`.

### Source resolution (`in` values)
- `input.<key>` — this turn: `input.query` = `ctx.query`; other keys come from
  `ctx.product_config`.
- `ctx.<path>` — `ctx.history` (list of `Message`), `ctx.user`, `ctx.session`,
  `ctx.previous_route`. Dotted paths do attribute/dict access: `ctx.user.name`.
- `<channel>` or `<channel>.<field>` — another step's output; `.field` does
  dict/attr access into it.
- A skipped/not-yet-run channel resolves to `None`.

### `when` predicate grammar (safe — parsed via ast, never `eval`)
Allowed: boolean `and`/`or`/`not`; comparisons `== != < > <= >=`; membership
`in` / `not in`; `len(x)`; literals (str/number/list/tuple); channel names.
Anything else raises "unsafe predicate". Names resolve to channel values (absent =
`None`). Examples:
- `"results"` — truthy check (retrieval found something)
- `"not results"` — the branch for no results
- `"'others' not in intent"` — intent is a list of category strings
- `"len(results) > 0"`

## The step contract

```python
async def run(inputs: dict, ctx: RequestContext, deps: HandlerDeps) -> Any:
    ...
    return value   # written to this step's `out` channel (omit `out` for side-effect-free/no-op)
```

- `inputs` — the resolved `in` map (keys are your param names, values already fetched).
- `ctx` — `RequestContext(query, session, user, product_config)`; `ctx.session.history`
  is `list[Message]`.
- `deps` — see below.

### `deps` (HandlerDeps) API
- `deps.runner.run(messages, *, response_format=None) -> RunResult`
  - `messages`: `list[Message]`, each `Message(role=Role.SYSTEM|USER|ASSISTANT, content=str)`.
  - `response_format`: `{"type": "json_schema", "schema": {...}}` to force structured
    JSON (read it back from `RunResult.text` and `json.loads` it).
  - `RunResult.text` = the model text; `.sources` = hosted-tool sources (response mode);
    `.tokens`.
- `deps.related_runner` — a cheaper runner for lightweight post-steps; falls back to
  `deps.runner` if unset. Use `deps.related_runner or deps.runner`.
- `deps.retriever.search(query, filters) -> list[Source]` — the workflow's retriever
  (from its `retriever.py`; `NullRetriever` returns `[]`).
- `deps.tools` — loaded typed tools (registered by the orchestration runtime, not called by steps).

### Types you will import
```python
from chatdemo.contracts import Message, RequestContext, Role, Response, ResponseState, Source
from chatdemo.handlers.base import HandlerDeps
```

## Returning a structured answer
- The normal path: a step returns text/list/dict to a channel, and `output` maps
  channels to `Response.answer` / `related_questions` / `sources`.
- A step may instead return a full `Response` object (e.g. a clarify) as the answer
  channel value; the engine detects it and returns it with meta set:
  ```python
  return Response(state=ResponseState.CLARIFY, answer="", clarify_question="...")
  ```

## Retriever (`retriever.py`)
```python
from chatdemo.contracts import Source
from chatdemo.handlers.base import NullRetriever, Retriever

def build(cfg: dict, folder) -> Retriever:
    # cfg = the manifest `retriever:` block; folder = this workflow's dir
    ...
    return MyRetriever(...)   # or FixtureRetriever / NullRetriever

class MyRetriever:
    async def search(self, query: str, filters: dict) -> list[Source]:
        ...
```
The registry loads `<folder>/retriever.py::build` and injects the result as
`deps.retriever`. No `retriever.py` -> `NullRetriever`. Keep an offline `fixture:`
fallback (build a `FixtureRetriever` when the endpoint env is unset).

## Post-processing retrieved sources (usually needed)

Raw `retriever.search(...)` results are almost never ready to answer over directly.
Add a **`process_sources` step** between `retrieve` and `answer` — DON'T do this
inside `retrieve` (keep retrieval = fetch, processing = a separate DAG node you can
test/skip independently). Decide which of these your retriever needs:

- **Dedup by URL + merge chunks** — a backend often returns several chunks of the
  SAME document. Keep the first occurrence and concatenate the `snippet`/text of
  later same-URL items, so each source is cited once. (Required for chunked
  retrievers; skip only if your backend already returns one row per document.)
- **Number the sources for citations** — the `answer` step should render sources as
  a numbered list (`[1] title: url\n{snippet}`) and instruct the model to cite
  inline as `[n]`. The UI shows sources numbered, so `[n]` lines up.
- **Optional**: truncate to top-k, drop sources with no URL, sort/rerank, or format
  domain-specific metadata (speakers, dates, session codes) into the context.

Wire it as its own channel so downstream reads the CLEAN list, not the raw one:

```yaml
steps:
  - {id: retrieve, in: {query: query, filters: keyword_filters}, out: results, when: "..."}
  - id: process_sources          # dedup / merge / number
    in: {results: results}
    out: sources
    when: "results"
  - {id: answer,  in: {query: query, sources: sources, history: ctx.history}, out: response, when: "sources"}
  - {id: related, in: {query: query, sources: sources}, out: related, when: "sources"}
output: {answer: response, sources: sources}   # publish the PROCESSED channel
```

```python
# steps/process_sources.py
from chatdemo.contracts import RequestContext, Source
from chatdemo.handlers.base import HandlerDeps

async def run(inputs: dict, ctx: RequestContext, deps: HandlerDeps) -> list[Source]:
    raw = inputs.get("results") or []
    by_url, ordered = {}, []
    for s in raw:
        key = s.url or s.title
        if key and key in by_url:
            existing = by_url[key]
            if s.snippet and s.snippet != existing.snippet:
                existing.snippet = f"{existing.snippet or ''}\n{s.snippet}".strip()
        else:
            if key:
                by_url[key] = s
            ordered.append(s)
    return ordered
```

Then in `answer`, build a numbered, citable context from `inputs["sources"]`:
```python
context = "\n\n".join(
    f"[{i}] {s.title or s.url}: {s.url or ''}\n{s.snippet or ''}".strip()
    for i, s in enumerate(inputs.get("sources") or [], 1)
)
# system prompt: "…cite the sources you use inline as [n]…"
```

## Action tools (`tools/<name>/`)

A workflow can offer typed **action tools** — LLM-visible, confirmable actions like
"submit feedback" or "file a ticket". They are NOT DAG steps and are NEVER called
from a step. The runtime registers them as owner-scoped orchestration functions and returns a
typed `PendingAction` before invoking any side-effecting tool.

### Where tools live — local vs shared
Each name in the manifest `tools:` is resolved by `load_tools`, searching in order:
1. `<this-workflow>/tools/<name>/` — a tool PRIVATE to this workflow.
2. `content/tools/<name>/` — a SHARED tool any handler can reference by name.

Local wins over shared. Put a tool in the shared top-level folder only when more than
one handler reuses it (e.g. `submit_feedback`); otherwise keep it private in the
workflow's own `tools/`.

### A tool is two files
```
tools/<name>/
  tool.yaml     # the ToolSpec
  handler.py    # the executor: def run(arguments, user) -> result
```

`tool.yaml`:
```yaml
name: submit_feedback
description: Submit a feedback request on the user's behalf.   # the LLM reads this to decide when to call
side_effecting: true        # true -> user confirms before the orchestration function runs
input_schema:               # JSON Schema the LLM fills; validated before run()
  type: object
  properties:
    summary: {type: string, description: Short summary}
    category: {type: string, enum: [bug, feature-request, question, other]}
  required: [summary, category]
```

`handler.py`:
```python
from typing import Any

def run(arguments: dict[str, Any], user: Any) -> dict[str, Any]:
    # arguments = the LLM-filled input (already validated against input_schema)
    # user      = the turn's user; do the real work (HTTP / MCP / db) and return a result
    return {"ticket_id": "FB-0001", **arguments}
```

### Declare + how it runs
```yaml
# workflow.yaml
tools:
  - submit_feedback        # private tools/ first, then shared content/tools/
```
At a turn: the user asks to perform the action → the specialist selects the owner-scoped
orchestration tool function → because `side_effecting: true`, the platform returns a CONFIRM
Response before invocation → the user replies "yes" → `run()` executes and its result
goes back to the model. Use `side_effecting: false` for read-only tools.
Same mechanism for a skill (declare `tools:` in SKILL.md frontmatter).

## Common failures
- **"cycle or missing dependency"** at load: a channel is read before any step
  produces it, or two steps depend on each other. Check `in`/`when` channel names.
- **A step's output is `None` downstream**: its `when` was false, or its `out` name
  doesn't match what the consumer reads.
- **Model returns non-JSON** when you passed `response_format`: guard with
  `try/except json.JSONDecodeError` and return a safe default.
- **Wrong file loaded**: `fn` must match the file stem under `steps/`.
