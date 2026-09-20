# ChatDemo Platform

Generic RAG chatbot platform built on an **agent orchestration toolkit**. One
typed entry workflow applies safety, confirmation, and routing, then invokes a
registered specialist function — either a deterministic **Workflow** or
model-driven **Skill** — and returns the uniform ChatDemo `Response` contract.

## Layout

```
src/chatdemo/          platform core (orchestration runtime, contracts, handlers, registry, API)
workflows/<name>/    code-orchestrated handlers  (workflow.yaml + steps/)
skills/<name>/       model-orchestrated handlers (SKILL.md + references/ + scripts/ + tools/)
tools/<name>/        shared, reusable typed tools (tool.yaml + handler)
```

## Develop

```bash
uv sync --extra dev
uv run pytest
uv run chatdemo            # start the chat service
```

## Conventions

- A **workflow** is described by `workflow.yaml` (name, description = router card, steps, runner, tool bindings).
- A **skill** is described by `SKILL.md` frontmatter (name, description = router card).
- `tools/` = typed orchestration functions; side-effecting calls require confirmation.
- `scripts/` = allowlisted helpers with a clean environment and timeout.
- Handlers are discovered from content and registered into `WorkflowBuilder` at startup.
