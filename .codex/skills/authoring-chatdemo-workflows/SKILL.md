---
name: authoring-chatdemo-workflows
description: >
  Scaffold and edit a chatdemo deterministic workflow (a DAG of steps) under
  chatdemo/src/chatdemo/content/workflows/<name>/. Use when a contributor wants to add a new
  workflow, add/modify steps, wire a retriever, add action tools, or debug the DAG
  (dependencies, parallel levels, cycles, when-conditions). Covers the manifest
  schema, the step signature, channel/dependency rules, and offline testing.
---

# Authoring a chatdemo workflow (DAG)

A **workflow** is code-orchestrated, deterministic RAG: an ordered set of **steps**
wired into a **DAG**. The engine derives dependencies from the channels each step
reads, runs independent steps in parallel, and returns a structured `Response`
verbatim. You author a workflow by dropping a folder under
`chatdemo/src/chatdemo/content/workflows/<name>/` — discovery and registration are automatic
(no code changes elsewhere).

Read `references/manifest-and-dag.md` for the full manifest schema + DAG semantics,
and `references/worked-example.md` for a complete copy-paste example.

## Mental model (read this first)

- A step writes exactly one **channel** (`out`) and reads channels/inputs (`in`).
- **Edges are inferred, not declared**: if step B's `in` (or `when`) references
  channel `x`, and step A produces `x`, then B depends on A. Steps with no edge
  between them run **concurrently** (same topological level).
- `input.*` = this turn's inputs (`input.query`), `ctx.*` = user/session/history
  (`ctx.history`, `ctx.user`), a bare name = another step's `out` channel.
- A gated step (`when:` false) is skipped and its channel stays absent (`None`).
- The DAG is validated at load time — a cycle or missing dependency raises.
- Action tools and confirmation live in the orchestration orchestration layer, NOT in
  steps. Steps are pure deterministic compute.

## Procedure

1. **Create the folder** `chatdemo/src/chatdemo/content/workflows/<name>/` with:
   ```
   workflow.yaml          # manifest: name, description, runner, retriever, steps, output, tools
   steps/<id>.py          # one file per step, each exposing `async def run(inputs, ctx, deps)`
   retriever.py           # OPTIONAL: this workflow's own retriever (build(cfg, folder))
   fixtures/*.json        # OPTIONAL: offline grounding data
   prompts/*.yaml         # OPTIONAL: externalized prompts
   ```

2. **Write `workflow.yaml`.** Required: `name`, `description`, `steps`. The
   `description` is the router card — write it so triage knows when to pick this
   workflow. Each step entry: `id`, optional `fn` (defaults to `id`), `in`, `out`,
   optional `when`/`after`/`retry`. Add an `output:` map (channel → Response field).
   See `references/manifest-and-dag.md`.

3. **Write each step** as `steps/<id>.py`:
   ```python
   from __future__ import annotations
   from chatdemo.contracts import Message, RequestContext, Role
   from chatdemo.handlers.base import HandlerDeps

   async def run(inputs: dict, ctx: RequestContext, deps: HandlerDeps) -> <value>:
       # inputs = resolved `in` map; return value is written to this step's `out`
       ...
       return value
   ```
   Call the model via `deps.runner.run(messages, response_format=SCHEMA)` (returns
   `RunResult` with `.text`); retrieve via `deps.retriever.search(query, filters)`
   (returns `list[Source]`). Reuse the cheap `deps.related_runner` for lightweight
   post-steps. Full deps API in the reference.

4. **(Optional) Retriever + source post-processing.** If the workflow retrieves,
   add `retriever.py` exposing `build(cfg, folder) -> Retriever` (a class with
   `async def search(query, filters) -> list[Source]`), and a `retriever:` block in
   the manifest whose keys are passed as `cfg`. Provide a `fixture:` fallback for
   offline. **Raw results usually need post-processing** — add a separate
   `process_sources` step between `retrieve` and `answer` (dedup by URL + merge
   chunks; number sources so `answer` can cite `[n]`). Do NOT bury this in
   `retrieve`. See "Post-processing retrieved sources" in the reference.

5. **(Optional) Action tools.** List tool names under `tools:` in the manifest. Each
   name resolves to a PRIVATE tool in the workflow's own `tools/<name>/` first, then
   a SHARED one in top-level `content/tools/<name>/`. A tool is two files —
   `tool.yaml` (name, description, `input_schema`, `side_effecting`) + `handler.py`
   (`def run(arguments, user)`). The runtime registers each as an owner-scoped orchestration
   function; side effects pause as `PendingAction` until the user confirms. Tools
   are never called from inside a step.
   See "Action tools" in the reference for the full pattern.

6. **Test offline** (no network, no keys — uses the mock model + fixtures):
   ```
   uv run pytest -q
   uv run python -c "import asyncio; from chatdemo.registry import Registry; \
     from chatdemo.runner.mock import MockModelClient; from chatdemo.agents_runtime import content_root; \
     from chatdemo.contracts import RequestContext, SessionState, User; \
     h = Registry(content_root(), MockModelClient()).discover()['<name>']; \
     r = asyncio.run(h.execute(RequestContext(query='hi', session=SessionState(session_id='t'), user=User(id='u')))); \
     print(r.model_dump())"
   ```
   Then `uv run ruff check .`.

## Cheat sheet

- **Step signature**: `async def run(inputs, ctx, deps) -> value` — return goes to `out`.
- **`in` sources**: `input.<k>` | `ctx.<path>` | `<channel>` | `<channel>.<field>`.
- **`when` grammar** (safe subset, NOT arbitrary Python): comparisons `== != < > <= >=`,
  `in` / `not in`, `and` / `or` / `not`, `len(x)`, truthiness, string/number/list
  literals, and channel names. Example: `"'others' not in intent"`, `"results"`,
  `"not results"`, `"len(results) > 0"`.
- **Parallel** = two steps with no channel edge between them (e.g. `answer` and
  `related` both reading `results`). Don't force ordering with `after` unless there
  is a real dependency.
- **Branch** = two steps writing the same `out` with mutually exclusive `when`
  (e.g. `answer` on `when: results`, `fallback` on `when: not results`).
- **`output` map**: `{answer: <channel>, related_questions: <channel>, sources: <channel>}`.
  A step may also return a full `Response` for the answer channel (e.g. a clarify).

## Checklist before you finish

- [ ] `workflow.yaml` has `name`, `description` (router-usable), `steps`, `output`.
- [ ] Every `in` channel name matches some step's `out` (or `input.*`/`ctx.*`) — a
      typo'd channel name is a silent missing dependency; the loader will raise.
- [ ] No cycles (the loader validates; if it raises "cycle or missing dependency",
      check that each channel is produced before it is read).
- [ ] `fn` set when the file name differs from the step `id`.
- [ ] Retriever has a `fixture:` fallback so it runs offline.
- [ ] If retrieval returns chunks, a `process_sources` step dedups by URL + numbers
      sources, and the `output`/`answer` read the PROCESSED `sources` channel (not raw
      `results`); `answer` cites `[n]`.
- [ ] No action-tool calls inside steps (tools go on `tools:` → orchestration registration).
- [ ] `uv run pytest -q` and `uv run ruff check .` pass.

## Repo layout & running commands

This skill lives at the repo root in `skills/authoring-chatdemo-workflows/`, alongside
`chatdemo/` (backend) and `ui/`. The repo already ships discovery symlinks so both
assistants pick it up automatically:
- `.claude/skills/authoring-chatdemo-workflows` (Claude Code)
- `.codex/skills/authoring-chatdemo-workflows` (Codex)

Both point back to the real folder under `skills/`; edit the skill there.

All `uv run …` commands below run from the **`chatdemo/`** project dir (that's where
the uv project + tests live), e.g. `cd chatdemo && uv run pytest -q`.
