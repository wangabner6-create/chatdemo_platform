# ChatDemo evaluation suite

This directory is the source of truth for the repeatable quality gate used by
the observability workflow.

## What is measured

`cases.yaml` contains stable bilingual examples and threshold policy. Every
example is executed through the real ChatDemo agent, so routing, specialist
execution, tool calls, retrieval, citations, errors, and latency are evaluated
as one path.

The first-stage evaluators are deterministic code checks:

- route and terminal-state accuracy;
- non-empty answer and error rate;
- expected RAG source retrieval;
- citation indices that resolve to returned sources;
- required concept coverage;
- per-case pass rate and p50/p95 latency.

Citation validity here means that a citation points to a source returned by the
agent. It does not yet judge whether that source semantically supports every
claim; an LLM-as-judge evaluator can be added as a later RLAIF signal.

## Run locally

```bash
uv run chatdemo-eval --require-baseline --version local-change
```

To publish the same run into Phoenix as a versioned Dataset/Experiment:

```bash
uv run chatdemo-eval --require-baseline --phoenix --version local-change
```

Reports are written to `evals/results/report.json` and
`evals/results/report.md`. A non-zero process exit means the quality gate
failed.

## Add or change a case

Give each example a permanent `id`, a user-style `query`, and the expected
route. Documentation cases should also declare expected source URIs and stable
answer concepts. A required-term item can be a string, or a list of semantic
alternatives such as `[router, routing, 路由]`.

Run the suite several times before accepting a new case so model variance does
not create a flaky gate. If the dataset or policy changes intentionally, review
the generated report and then record the accepted baseline:

```bash
uv run chatdemo-eval --record-baseline --version reviewed-baseline
```

`baseline.json` must be reviewed like source code. Do not update it merely to
make a regression pass.

## CI configuration

The GitHub Actions workflow always runs deterministic tests. The live agent
gate activates when these repository settings exist:

- secrets: `CHATDEMO_EVAL_BASE_URL`, `CHATDEMO_EVAL_API_KEY`;
- variables: `CHATDEMO_EVAL_MODEL`, `CHATDEMO_EVAL_EMBED_MODEL`;
- optional Phoenix secrets: `CHATDEMO_EVAL_PHOENIX_ENDPOINT`,
  `CHATDEMO_EVAL_PHOENIX_URL`, `CHATDEMO_EVAL_PHOENIX_API_KEY`.

On a pull request, the workflow uploads both reports, adds the Markdown report
to the run summary, updates a single bot comment, and fails the check when any
absolute threshold or baseline-regression rule is violated.
