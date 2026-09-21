# ChatDemo Platform

A per-product-configurable **RAG chatbot platform** on an **agent orchestration
toolkit**. One typed entry workflow applies safety, routes each query, and
invokes one registered specialist function: either a **Workflow**
(code-orchestrated deterministic RAG — a DAG of steps) or a **Skill**
(model-driven, with provider-hosted web search and bundled tools/scripts). Every
handler owns its retrieval and action tools; answers share one uniform contract.

```
chatdemo/   backend service (FastAPI) — triage, workflows, skills, tools, retrieval
ui/       chat UI (React + Vite + Tailwind + shadcn/ui) — sessions, markdown, sources, feedback
skills/   authoring-chatdemo-workflows — the skill Claude Code / Codex use to add workflows
```

## Architecture at a glance

- **Entry workflow** — `WorkflowBuilder` assembles a typed `chatdemo_workflow` entry
  function plus one registered function per specialist and tool. Pin one handler
  with `CHATDEMO_ROUTE` to skip model routing.
- **Triage router** — a cheap configured model selects only among the enabled
  specialist cards. It never owns or executes tools.
- **Workflows** (`chatdemo/.../content/workflows/<name>/`) — a **DAG engine**: each
  step declares `in`/`out`/`when`; edges are inferred from channels, independent
  steps run in parallel, cycles are rejected at load. Each workflow owns its
  `retriever.py`; declared tools become owner-scoped orchestration functions. Side
  effects pause as a typed `PendingAction` until the user confirms.
- **Skills** (`chatdemo/.../content/skills/<name>/`) — registered specialist
  functions with progressive-disclosure references, Responses hosted web search,
  and guarded `scripts/` execution (allowlist + clean env + timeout). Example:
  `demo-assistant`.
- **Per-role models** — one model per runtime role, each independently `completion`
  or `response`: `CHATDEMO_MODEL` (required base, everything falls back to it),
  `CHATDEMO_ROUTER_MODEL` (triage), `CHATDEMO_SKILL_MODEL` (skills),
  `CHATDEMO_WF_MODEL` (workflow steps), `CHATDEMO_RELATED_MODEL` (related-questions),
  each with a matching `*_API_STYLE`. A `web_search: true` skill is forced onto
  Responses. `ChatSafeSession` stores only provider-neutral text messages, so API
  styles can share history without persisting tool/reasoning artifacts.
- **Redis Stack** — multi-turn sessions, pending approvals, and user feedback
  (RediSearch `feedbackchatbot` index) all live in one Redis instance.

## Quick start (Docker Compose — recommended)

```bash
cp .env.example .env          # then fill in the provider block to go live
docker compose up --build
# open http://localhost
```

Services: `redis` (Redis Stack — sessions + pending + feedback, appendonly
volume) · `backend` (FastAPI on :8000) · `ui` (nginx serving `ui/dist` on :80,
reverse-proxying `/chat`, `/session`, `/feedback`, `/health`). With no provider
set the backend runs a credential-free **mock model** offline.

### Prebuilt UI bundle (`ui/dist/`)

The UI image only **copies** a prebuilt `ui/dist/`; it does not build inside
Docker, to keep the image small and the build fast. So `ui/dist/` is
**committed to the repo** — a fresh clone can `docker compose up --build` on
any machine with no `npm install` at all.

Rebuild the bundle only when you change the UI:

```bash
cd ui && npm install && npm run build && cd ..   # -> ui/dist/
git rm -r --cached ui/dist && git add -f ui/dist  # drop stale content-hashed files, re-add
git commit -m "chore(ui): rebuild dist" && git push
```

`ui/dist/` stays in `ui/.gitignore`, so new build output is never staged by
accident — only the explicit `git add -f` above updates it. The `git rm --cached`
step clears the previous content-hashed filenames (e.g. `index-608StOwY.js`) so
they don't pile up across rebuilds.

### Changing exposed ports

Ports in `docker-compose.yml` are `host:container`; edit only the host (left)
side to remap without touching any code — nginx→backend (`backend:8000`) and
backend→redis (`redis:6379`) talk over the compose network on the container
ports. e.g. backend `"8089:8000"`, and add `ports: ["6479:6379"]` under `redis`
to reach it on the host (redis has no published port by default). Then
`docker compose up -d` (no `--build` needed for a port-only change).

## Quick start (local dev, no Docker)

Run the backend and the UI as two separate processes — best for active development
(hot reload on both sides).

**Terminal 1 — backend:**

```bash
cd chatdemo
uv sync --extra dev --extra observability
uv run pytest           # offline test suite (mock model + fixtures)
uv run ruff check .
uv run chatdemo          # http://localhost:8000  (mock model unless CHATDEMO_BASE_URL is set)
```

To hit a real provider instead of the mock model, export the provider block
before starting (or `set -a; . ../.env; set +a` if you've filled one in):

```bash
export CHATDEMO_BASE_URL=<openai-compatible endpoint>
export CHATDEMO_API_KEY=<key>
export CHATDEMO_MODEL=<model-id>
uv run chatdemo
```

For the checked-in docs agent, a fully local real-model profile can use Ollama:

```bash
ollama pull qwen3:4b-instruct
ollama pull qwen3-embedding:0.6b
set -a; . ../.env; set +a
uv run python scripts/ingest_docs.py   # rebuild after changing corpus or embedding model
uv run chatdemo
```

The machine-local `.env` is gitignored. This profile keeps the mock client only
for the offline test suite; live requests run through the Qwen router, skill
agent, tool loop, and docs retriever.

### Phoenix tracing

With the observability extra installed, start the local trace database/UI before
the backend:

```bash
cd chatdemo
PHOENIX_WORKING_DIR=../.phoenix uv run phoenix serve   # http://localhost:6006
```

Set `CHATDEMO_TRACING=on`, `CHATDEMO_PHOENIX_ENDPOINT`, and
`CHATDEMO_TRACE_PROJECT` in `.env`. Each response then carries the same 32-character
trace ID exported to Phoenix, and thumbs feedback stores that ID together with
the selected route and model. Existing live SSE steps remain available in the
chat UI.

### Quality evaluation and regression gate

The checked-in evaluation set at `chatdemo/evals/cases.yaml` turns traces into
repeatable quality signals. It covers bilingual routing, docs retrieval,
citation validity, expected concepts, errors, and latency. Run it against the
same real provider profile as the app:

```bash
cd chatdemo
set -a; . ../.env; set +a
uv run chatdemo-eval --phoenix
```

The command writes `evals/results/report.json` plus a PR-friendly
`evals/results/report.md`, compares the run with `evals/baseline.json`, exits
non-zero when a quality floor or allowed regression is breached, uploads a
versioned Phoenix Dataset/Experiment, and attaches code-evaluator annotations
to each originating trace. Use `--record-baseline` only after reviewing an
intentional dataset/model change; a failing run is never accepted as a new
baseline.

`.github/workflows/observability-eval.yml` runs on every pull request and every
update to `main`. The hosted runner starts pinned Ollama and uses the real
`qwen3:4b-instruct` plus `qwen3-embedding:0.6b` models, so the live gate needs no
external model secret and cannot silently pass by skipping evaluation or using
the mock client. CI rebuilds the docs index, adds the report to the Actions
summary, uploads JSON/Markdown artifacts, and creates or updates one bot
comment on the PR.

Phoenix export is optional for CI because the tracing server must be reachable
from GitHub. Configure `CHATDEMO_EVAL_PHOENIX_ENDPOINT`,
`CHATDEMO_EVAL_PHOENIX_URL`, and `CHATDEMO_EVAL_PHOENIX_API_KEY` repository
secrets to publish traces and a versioned Dataset/Experiment. The protected
`main` branch requires code/test, deployable-image, and evaluation checks; a
failed threshold therefore blocks merging instead of becoming a passive
report.

**Terminal 2 — UI:**

```bash
cd ui
npm install              # plain public registry, no special access needed
npm run dev              # http://localhost:5173 (proxies /chat, /health, /session, /feedback -> :8000)
```

Open `http://localhost:5173`, pick any user name to log in (no real auth), and
chat. Blank `CHATDEMO_REDIS_URL` (the default) uses local SQLite + in-memory
storage instead of Redis, so nothing else needs to be running.

Point the backend at a real provider + services via env (see `.env.example`
for the full, commented list):

```bash
CHATDEMO_MODEL=<model>                      # REQUIRED base; every role falls back to it
CHATDEMO_API_STYLE=completion|response      # base style (each role falls back to this)
CHATDEMO_ROUTER_MODEL=<model>   CHATDEMO_ROUTER_API_STYLE=completion|response   # triage
CHATDEMO_SKILL_MODEL=<model>    CHATDEMO_SKILL_API_STYLE=completion|response    # all skills
CHATDEMO_WF_MODEL=<model>       CHATDEMO_WF_API_STYLE=completion|response       # workflow steps (style empty -> manifest)
CHATDEMO_RELATED_MODEL=<model>  CHATDEMO_RELATED_API_STYLE=completion|response  # related-questions
CHATDEMO_BASE_URL=<openai-compatible>  CHATDEMO_API_KEY=<key>
CHATDEMO_REDIS_URL=redis://localhost:6379/0 # Redis Stack; blank -> SQLite + in-memory
CHATDEMO_SAFETY=on|off                      # LLM safety input guardrail
CHATDEMO_SKILL_SCRIPTS=on|off               # allow skills to run bundled scripts/
CHATDEMO_ENABLED=<csv>   CHATDEMO_ROUTE=<one handler>   # content selection / pin a handler
```

Each workflow can also define its own env-driven overrides in its own
`workflow.yaml` (see `skills/authoring-chatdemo-workflows/`).

## Feedback

The UI writes feedback straight to Redis (no LLM, no confirmation):

- **thumb 👍/👎** on any reply → `POST /feedback` with the question + answer + vote;
- **sidebar box** → free-text feedback with an optional vote.

Typing feedback *in chat* instead routes through the `submit_feedback` action
tool (the model infers the vote/summary, confirmation applies). All records land
in the RediSearch `feedbackchatbot:<id>` index — query e.g.
`redis-cli FT.SEARCH feedbackchatbot '@feedback:{down}'`.

## Authoring a workflow

Adding or editing a workflow is covered by the **`authoring-chatdemo-workflows`**
skill (`skills/authoring-chatdemo-workflows/`), discoverable by Claude Code and
Codex via the `.claude/` and `.codex/` symlinks. It documents the manifest
schema, the step signature, channel/DAG rules, retrievers, action tools, and
offline testing.
