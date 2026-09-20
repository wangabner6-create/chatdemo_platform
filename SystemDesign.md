# ChatDemo Platform System Design

ChatDemo is a configurable RAG and action platform built on an **agent
orchestration toolkit**. Products contribute deterministic workflows, model-driven
skills, retrievers, and typed tools as content. The platform discovers that
content and assembles one executable orchestration workflow at service startup.

## Goals

- Keep one HTTP and response contract across products.
- Let each domain own its retrieval and deterministic processing pipeline.
- Isolate tools to the workflow or skill that declared them.
- Require explicit confirmation before side effects.
- Support multi-turn history through Redis or local SQLite.
- Run offline with fixtures and a credential-free mock model.

## Runtime Architecture

```text
FastAPI /chat or /chat/stream
        |
        v
chatdemo_workflow (typed AgentRequest -> Response)
        |
        +-- pending confirmation? -> owner-scoped tool function
        +-- safety guardrail
        +-- fixed route or model router
        |
        v
registered specialist function
        |
        +-- deterministic WorkflowHandler DAG
        |      +-- workflow-owned runner
        |      +-- workflow-owned retriever
        |      +-- parallel step levels
        |
        +-- model-driven Skill
               +-- progressive reference tools
               +-- optional hosted web search
               +-- optional guarded scripts
               +-- skill-owned typed tools
```

`chatdemo.nemo_runtime.build_runtime()` uses `WorkflowBuilder` to register:

1. one function for every loaded tool;
2. one function for every workflow or skill specialist;
3. one `chatdemo_workflow` entry function for safety, routing, approvals, and
   specialist dispatch.

The deterministic workflow engine remains domain logic. The orchestration
toolkit owns the executable graph, nested function invocation, component
lifecycle, and workflow-level observability.

## Content Model

### Workflows

`content/workflows/<name>/workflow.yaml` describes a deterministic DAG. Each step
declares input channels, an output channel, optional predicates, ordering, and
retry count. Dependencies are inferred from channel reads; independent steps run
concurrently. Cycles and missing dependencies fail at startup.

A workflow may also define:

- `retriever.py` for domain-specific retrieval;
- local fixtures for offline execution;
- private tools under `tools/<name>/`;
- references to shared tools under `content/tools/`.

### Skills

`content/skills/<name>/SKILL.md` supplies the router card and system instructions.
A skill may expose progressive-disclosure references, guarded scripts, hosted
Responses web search, and typed local/shared tools. The runtime registers these
capabilities as functions scoped to that skill.

### Tools and Confirmation

Each tool contains `tool.yaml` plus `handler.py`. `tool.yaml` defines the model-
visible name, description, JSON input schema, and whether the operation is side
effecting.

Read-only tools execute immediately. A side-effecting call returns a typed
`PendingAction`; the pending action is stored by session. A later affirmative
turn invokes the exact owner-scoped orchestration function with server-authoritative user
identity. A negative turn cancels it without executing the handler.

## Models and Providers

ChatDemo uses one OpenAI-compatible provider channel and supports Chat Completions
or Responses per role:

- `CHATDEMO_ROUTER_MODEL`: top-level route selection;
- `CHATDEMO_SKILL_MODEL`: model-driven skills and tool loops;
- `CHATDEMO_WF_MODEL`: workflow step runners;
- `CHATDEMO_RELATED_MODEL`: lightweight related-question generation.

Every role falls back to `CHATDEMO_MODEL`. A skill with `web_search: true` uses the
Responses API and the configured hosted-search tool type.

## State and Storage

`ChatSafeSession` stores only normalized user/assistant/system/developer text
messages. Provider reasoning items, function calls, and hosted-tool artifacts are
turn-local and never persisted.

- Redis configured: sessions and pending actions are shared across replicas.
- Redis absent: sessions use SQLite and pending actions use process memory.
- UI history replacement rewrites the same backend session and clears stale
  pending actions.

## API Contract

- `POST /chat`: complete typed `Response`.
- `POST /chat/stream`: SSE trace events followed by response chunks and metadata.
- `POST /session/{id}/replace`: replace persisted conversation history.
- `POST /feedback`: direct UI feedback path.
- `GET /feedback`: token-gated administrative feedback view.

`Response` always contains answer text, structured sources, related questions,
state (`answer`, `clarify`, or `confirm`), optional pending action, and
route/latency/token metadata.

## Observability and Evaluation

Every live turn has one OpenTelemetry trace ID shared by the API response,
feedback record, and Phoenix trace. The native NeMo exporter records the entry
workflow and nested safety, router, specialist, model, tool, embedding, and
retrieval spans. Phoenix is the durable trace store and replay UI; the SSE trace
remains a live debugging view rather than the system of record.

`chatdemo.evaluation` supplies the quality layer above those traces:

1. `evals/cases.yaml` is the reviewed bilingual dataset and quality policy.
2. A real agent run produces route, answer, sources, latency, model, and trace ID.
3. Deterministic evaluators score routing, retrieval hit, citation structure,
   expected source citation, required concepts, state, errors, and latency.
4. The JSON/Markdown report is compared with `evals/baseline.json`; floors and
   allowed deltas form a CI gate.
5. The same cases and scores are persisted as a versioned Phoenix Experiment,
   and scores are also attached to the original traces as code annotations.

The application fingerprint hashes behavior-defining source, content, index,
dataset, and model settings. CI normally uses the commit SHA as the visible
experiment version. LLM-as-a-judge evaluation can be added as another evaluator
later; it does not replace the deterministic route/retrieval checks.

## Extensibility

Adding a workflow or skill requires no central router code change. Discovery uses
the content descriptions as router cards, and runtime assembly automatically
creates the corresponding orchestration functions and scoped tools. MCP, HTTP, fixture,
or local retrievers can implement the same `Retriever` protocol.
