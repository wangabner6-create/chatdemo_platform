"""Offline quality evaluation and regression gate for ChatDemo.

The same run serves three audiences:

* developers get a JSON report and a compact Markdown summary;
* CI gets a deterministic exit code based on quality floors and baseline deltas;
* Phoenix gets a versioned dataset, experiment runs, and trace annotations.

The evaluators are intentionally code based. They measure routing, retrieval,
citation structure, required answer concepts, errors, and latency without using
the model-under-test as its own judge. Semantic LLM-as-a-judge scores can be
added later without changing the dataset or report contract.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import statistics
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .config import Settings
from .contracts import Response, ResponseState, User
from .nemo_runtime import run_agent

AgentCallable = Callable[[str, str, User | None], Awaitable[Response]]

_CITATION_RE = re.compile(r"\[([1-9]\d*)\]")
_RATE_METRICS = {
    "route_accuracy",
    "state_accuracy",
    "answer_present_rate",
    "retrieval_hit_rate",
    "citation_validity",
    "expected_source_citation_rate",
    "term_coverage",
    "case_pass_rate",
    "error_rate",
}
_METRIC_ORDER = (
    "route_accuracy",
    "retrieval_hit_rate",
    "citation_validity",
    "expected_source_citation_rate",
    "term_coverage",
    "answer_present_rate",
    "state_accuracy",
    "case_pass_rate",
    "error_rate",
    "latency_p50_ms",
    "latency_p95_ms",
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class GatePolicy(BaseModel):
    """Quality policies applied both locally and in CI."""

    minimum: dict[str, float] = Field(default_factory=dict)
    maximum: dict[str, float] = Field(default_factory=dict)
    max_drop_from_baseline: dict[str, float] = Field(default_factory=dict)
    max_relative_increase: dict[str, float] = Field(default_factory=dict)


class EvalCase(BaseModel):
    id: str
    query: str
    expected_route: str
    expected_state: ResponseState = ResponseState.ANSWER
    expected_sources: list[str] = Field(default_factory=list)
    # A string is an exact required term. A nested list is one semantic concept
    # with acceptable lexical alternatives, e.g. [router, routing, 路由].
    required_terms: list[str | list[str]] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class EvalDataset(BaseModel):
    schema_version: int = 1
    name: str
    description: str = ""
    policy: GatePolicy = Field(default_factory=GatePolicy)
    cases: list[EvalCase]


class CaseResult(BaseModel):
    case_id: str
    query: str
    expected_route: str
    route: str | None = None
    state: str | None = None
    model: str | None = None
    trace_id: str | None = None
    answer: str = ""
    sources: list[str] = Field(default_factory=list)
    started_at: str
    completed_at: str
    latency_ms: float
    metrics: dict[str, float | None] = Field(default_factory=dict)
    error: str | None = None


class GateResult(BaseModel):
    passed: bool
    violations: list[str] = Field(default_factory=list)


class Baseline(BaseModel):
    schema_version: int = 1
    dataset_name: str
    dataset_digest: str
    version: str
    created_at: str
    metrics: dict[str, float]


class PhoenixPublishResult(BaseModel):
    dataset_id: str
    dataset_version_id: str
    experiment_id: str
    experiment_url: str | None = None
    trace_annotations: int = 0
    warnings: list[str] = Field(default_factory=list)


class EvaluationReport(BaseModel):
    schema_version: int = 1
    run_id: str
    version: str
    dataset_name: str
    dataset_digest: str
    started_at: str
    completed_at: str
    provider: dict[str, Any]
    metrics: dict[str, float]
    baseline: Baseline | None = None
    gate: GateResult
    cases: list[CaseResult]
    phoenix: PhoenixPublishResult | None = None


def load_dataset(path: Path) -> EvalDataset:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    dataset = EvalDataset.model_validate(data)
    ids = [case.id for case in dataset.cases]
    if not ids:
        raise ValueError(f"{path}: dataset has no cases")
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: case ids must be unique")
    return dataset


def dataset_digest(dataset: EvalDataset) -> str:
    payload = json.dumps(
        dataset.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def application_fingerprint(root: Path, dataset: EvalDataset, settings: Settings) -> str:
    """Hash behavior-defining source, content, index, dataset, and model settings."""

    digest = hashlib.sha256()
    source_root = root / "src" / "chatdemo"
    for path in sorted(source_root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    digest.update(dataset_digest(dataset).encode())
    model_profile = {
        "model": settings.model,
        "router_model": settings.router_model,
        "skill_model": settings.skill_model,
        "api_style": settings.api_style,
        "router_api_style": settings.router_api_style,
        "skill_api_style": settings.skill_api_style,
    }
    digest.update(json.dumps(model_profile, sort_keys=True).encode())
    return digest.hexdigest()[:12]


def _mean(values: Iterable[float | None]) -> float | None:
    kept = [float(value) for value in values if value is not None]
    return statistics.fmean(kept) if kept else None


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def score_response(case: EvalCase, response: Response) -> dict[str, float | None]:
    returned_urls = [source.url or "" for source in response.sources]
    expected = set(case.expected_sources)
    citations = [int(match) for match in _CITATION_RE.findall(response.answer)]

    retrieval_hit: float | None = None
    citation_validity: float | None = None
    expected_source_cited: float | None = None
    if expected:
        retrieval_hit = float(bool(expected.intersection(returned_urls)))
        citation_validity = float(
            bool(citations) and all(1 <= citation <= len(returned_urls) for citation in citations)
        )
        expected_source_cited = float(
            any(
                1 <= citation <= len(returned_urls) and returned_urls[citation - 1] in expected
                for citation in citations
            )
        )

    term_coverage: float | None = None
    if case.required_terms:
        answer_lower = response.answer.casefold()
        term_coverage = sum(
            any(
                alternative.casefold() in answer_lower
                for alternative in ([requirement] if isinstance(requirement, str) else requirement)
            )
            for requirement in case.required_terms
        ) / len(
            case.required_terms,
        )

    metrics: dict[str, float | None] = {
        "route_accuracy": float(response.meta.route == case.expected_route),
        "state_accuracy": float(response.state == case.expected_state),
        "answer_present_rate": float(bool(response.answer.strip())),
        "retrieval_hit_rate": retrieval_hit,
        "citation_validity": citation_validity,
        "expected_source_citation_rate": expected_source_cited,
        "term_coverage": term_coverage,
        "error_rate": 0.0,
    }
    required = [
        metrics["route_accuracy"],
        metrics["state_accuracy"],
        metrics["answer_present_rate"],
        retrieval_hit,
        citation_validity,
        expected_source_cited,
        term_coverage,
    ]
    metrics["case_pass_rate"] = float(
        all(value >= 0.999 for value in required if value is not None)
    )
    return metrics


def _error_metrics(case: EvalCase) -> dict[str, float | None]:
    return {
        "route_accuracy": 0.0,
        "state_accuracy": 0.0,
        "answer_present_rate": 0.0,
        "retrieval_hit_rate": 0.0 if case.expected_sources else None,
        "citation_validity": 0.0 if case.expected_sources else None,
        "expected_source_citation_rate": 0.0 if case.expected_sources else None,
        "term_coverage": 0.0 if case.required_terms else None,
        "case_pass_rate": 0.0,
        "error_rate": 1.0,
    }


async def evaluate_dataset(
    dataset: EvalDataset,
    *,
    agent: AgentCallable = run_agent,
    concurrency: int = 1,
    run_id: str | None = None,
) -> list[CaseResult]:
    run_id = run_id or f"{_utc_now():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    semaphore = asyncio.Semaphore(max(1, concurrency))
    progress_lock = asyncio.Lock()
    completed = 0

    async def execute(case: EvalCase) -> CaseResult:
        nonlocal completed
        response: Response | None = None
        error: str | None = None
        async with semaphore:
            # Measure only this case's execution. Time spent waiting for the
            # concurrency semaphore is queueing delay, not agent latency.
            started = _utc_now()
            started_clock = time.perf_counter()
            try:
                response = await agent(
                    case.query,
                    f"eval-{run_id}-{case.id}",
                    User(id=f"eval:{case.id}", name="ChatDemo evaluator"),
                )
            except Exception as exc:  # noqa: BLE001 - evaluator records per-case failures
                error = f"{type(exc).__name__}: {exc}"
            completed_at = _utc_now()
            latency_ms = round((time.perf_counter() - started_clock) * 1000, 1)
        if response is None:
            result = CaseResult(
                case_id=case.id,
                query=case.query,
                expected_route=case.expected_route,
                started_at=started.isoformat(),
                completed_at=completed_at.isoformat(),
                latency_ms=latency_ms,
                metrics=_error_metrics(case),
                error=error,
            )
        else:
            result = CaseResult(
                case_id=case.id,
                query=case.query,
                expected_route=case.expected_route,
                route=response.meta.route,
                state=response.state.value,
                model=response.meta.model,
                trace_id=response.meta.trace_id,
                answer=response.answer,
                sources=[source.url or "" for source in response.sources],
                started_at=started.isoformat(),
                completed_at=completed_at.isoformat(),
                latency_ms=latency_ms,
                metrics=score_response(case, response),
            )
        async with progress_lock:
            completed += 1
            status = "PASS" if result.metrics["case_pass_rate"] == 1 else "FAIL"
            print(
                f"[{completed}/{len(dataset.cases)}] {status} {case.id} "
                f"route={result.route or '-'} latency={result.latency_ms:.0f}ms",
                flush=True,
            )
        return result

    return list(await asyncio.gather(*(execute(case) for case in dataset.cases)))


def aggregate_metrics(results: list[CaseResult]) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for name in _RATE_METRICS:
        value = _mean(result.metrics.get(name) for result in results)
        if value is not None:
            metrics[name] = round(value, 6)
    latencies = [result.latency_ms for result in results]
    metrics["latency_p50_ms"] = round(_percentile(latencies, 0.50), 1)
    metrics["latency_p95_ms"] = round(_percentile(latencies, 0.95), 1)
    return metrics


def compare_metrics(
    metrics: dict[str, float],
    policy: GatePolicy,
    baseline: Baseline | None,
    *,
    current_dataset_digest: str,
) -> GateResult:
    violations: list[str] = []
    for name, minimum in policy.minimum.items():
        current = metrics.get(name)
        if current is None:
            violations.append(f"{name}: metric missing (minimum {minimum})")
        elif current < minimum:
            violations.append(f"{name}: {current:.4f} is below minimum {minimum:.4f}")
    for name, maximum in policy.maximum.items():
        current = metrics.get(name)
        if current is None:
            violations.append(f"{name}: metric missing (maximum {maximum})")
        elif current > maximum:
            violations.append(f"{name}: {current:.4f} exceeds maximum {maximum:.4f}")

    if baseline is not None:
        if baseline.dataset_digest != current_dataset_digest:
            violations.append(
                "dataset changed: record a reviewed baseline for the new dataset version"
            )
        for name, allowed_drop in policy.max_drop_from_baseline.items():
            current = metrics.get(name)
            previous = baseline.metrics.get(name)
            if current is not None and previous is not None and previous - current > allowed_drop:
                violations.append(
                    f"{name}: dropped {previous - current:.4f} "
                    f"(allowed {allowed_drop:.4f}; baseline {previous:.4f})"
                )
        for name, allowed_ratio in policy.max_relative_increase.items():
            current = metrics.get(name)
            previous = baseline.metrics.get(name)
            if current is None or previous is None:
                continue
            if previous <= 0:
                if current > 0:
                    violations.append(f"{name}: increased from zero to {current:.4f}")
                continue
            ratio = (current - previous) / previous
            if ratio > allowed_ratio:
                violations.append(
                    f"{name}: increased {ratio:.1%} "
                    f"(allowed {allowed_ratio:.1%}; baseline {previous:.1f})"
                )
    return GateResult(passed=not violations, violations=violations)


def policy_with_overrides(
    policy: GatePolicy,
    *,
    latency_p95_ceiling_ms: float | None = None,
) -> GatePolicy:
    """Return an isolated policy with environment-specific runtime ceilings."""

    effective = policy.model_copy(deep=True)
    if latency_p95_ceiling_ms is not None:
        if latency_p95_ceiling_ms <= 0:
            raise ValueError("latency p95 ceiling must be greater than zero")
        effective.maximum["latency_p95_ms"] = latency_p95_ceiling_ms
    return effective


def _metric_label(name: str, value: float) -> str | None:
    if name == "error_rate":
        return "pass" if value == 0 else "fail"
    if name in _RATE_METRICS:
        return "pass" if value >= 0.999 else "fail"
    return None


def _metric_explanation(case: EvalCase, result: CaseResult, name: str) -> str:
    if name == "route_accuracy":
        return f"expected {case.expected_route}; received {result.route or 'none'}"
    if name == "retrieval_hit_rate":
        return f"expected any of {case.expected_sources}; received {result.sources}"
    if name == "citation_validity":
        return "all bracket citations must point to a returned source"
    if name == "expected_source_citation_rate":
        return "at least one citation must point to an expected source"
    if name == "term_coverage":
        return f"required terms: {case.required_terms}"
    if name == "case_pass_rate":
        return "all applicable code evaluators must pass"
    return name.replace("_", " ")


def publish_to_phoenix(
    *,
    base_url: str,
    dataset: EvalDataset,
    report: EvaluationReport,
) -> PhoenixPublishResult:
    """Publish a versioned dataset, experiment, evals, and trace annotations."""

    from phoenix.client import Client

    client = Client(base_url=base_url.rstrip("/"))
    examples = [
        {
            "id": case.id,
            "input": {"query": case.query},
            "output": {
                "route": case.expected_route,
                "state": case.expected_state.value,
                "sources": case.expected_sources,
                "required_terms": case.required_terms,
            },
            "metadata": {"case_id": case.id, "tags": case.tags},
        }
        for case in dataset.cases
    ]
    phoenix_dataset = client.datasets.create_dataset(
        name=dataset.name,
        examples=examples,
        dataset_description=dataset.description,
        timeout=30,
    )
    experiment = client.experiments.create(
        dataset_id=phoenix_dataset.id,
        dataset_version_id=phoenix_dataset.version_id,
        experiment_name=report.version,
        experiment_description="ChatDemo route, retrieval, citation, and latency regression run",
        experiment_metadata={
            "run_id": report.run_id,
            "dataset_digest": report.dataset_digest,
            "model": report.provider.get("model"),
            "router_model": report.provider.get("router_model"),
        },
        timeout=30,
    )
    examples_by_case = {
        str(example.get("metadata", {}).get("case_id")): example["node_id"]
        for example in phoenix_dataset.examples
    }
    cases_by_id = {case.id: case for case in dataset.cases}
    annotations = 0
    warnings: list[str] = []
    for result in report.cases:
        example_id = examples_by_case.get(result.case_id)
        if example_id is None:
            warnings.append(f"{result.case_id}: Phoenix dataset example was not found")
            continue
        run = client.experiments.log_run(
            experiment_id=experiment["id"],
            dataset_example_id=example_id,
            output={
                "route": result.route,
                "state": result.state,
                "answer": result.answer,
                "sources": result.sources,
                "model": result.model,
            },
            start_time=datetime.fromisoformat(result.started_at),
            end_time=datetime.fromisoformat(result.completed_at),
            trace_id=result.trace_id,
            error=result.error,
            timeout=30,
        )
        case = cases_by_id[result.case_id]
        for name, value in result.metrics.items():
            if value is None:
                continue
            explanation = _metric_explanation(case, result, name)
            client.experiments.log_evaluation(
                experiment_run_id=run["id"],
                name=name,
                annotator_kind="CODE",
                score=value,
                label=_metric_label(name, value),
                explanation=explanation,
                metadata={"version": report.version, "case_id": case.id},
                trace_id=result.trace_id,
                timeout=30,
            )
            if result.trace_id and name != "latency_ms":
                try:
                    client.traces.add_trace_annotation(
                        trace_id=result.trace_id,
                        annotation_name=name,
                        annotator_kind="CODE",
                        score=value,
                        label=_metric_label(name, value),
                        explanation=explanation,
                        metadata={
                            "version": report.version,
                            "run_id": report.run_id,
                            "dataset": dataset.name,
                            "case_id": case.id,
                        },
                        identifier=f"chatdemo-eval:{report.version}:{case.id}:{name}",
                        sync=True,
                    )
                    annotations += 1
                except Exception as exc:  # noqa: BLE001 - experiment data remains useful
                    warnings.append(f"{case.id}/{name}: trace annotation failed: {exc}")

    experiment_url = client.experiments.get_experiment_url(
        phoenix_dataset.id,
        experiment["id"],
    )
    return PhoenixPublishResult(
        dataset_id=phoenix_dataset.id,
        dataset_version_id=phoenix_dataset.version_id,
        experiment_id=experiment["id"],
        experiment_url=experiment_url,
        trace_annotations=annotations,
        warnings=warnings,
    )


def _format_metric(name: str, value: float | None) -> str:
    if value is None:
        return "—"
    if name.endswith("_ms"):
        return f"{value:,.0f} ms"
    if name in _RATE_METRICS:
        return f"{value:.1%}"
    return f"{value:.4f}"


def render_markdown(report: EvaluationReport, policy: GatePolicy) -> str:
    status = "PASS ✅" if report.gate.passed else "FAIL ❌"
    lines = [
        "# ChatDemo observability evaluation",
        "",
        f"**Gate:** {status}  ",
        f"**Version:** `{report.version}`  ",
        f"**Run:** `{report.run_id}`  ",
        f"**Dataset:** `{report.dataset_name}` (`{report.dataset_digest[:12]}`)  ",
        f"**Model:** `{report.provider.get('model', '')}`",
        "",
        "| Metric | Current | Baseline | Delta |",
        "| --- | ---: | ---: | ---: |",
    ]
    names = [name for name in _METRIC_ORDER if name in report.metrics]
    names.extend(sorted(set(report.metrics) - set(names)))
    for name in names:
        current = report.metrics[name]
        previous = report.baseline.metrics.get(name) if report.baseline else None
        delta = current - previous if previous is not None else None
        lines.append(
            f"| `{name}` | {_format_metric(name, current)} | "
            f"{_format_metric(name, previous)} | {_format_metric(name, delta)} |"
        )

    lines.extend(
        [
            "",
            "| Case | Expected route | Actual route | Pass | Latency | Trace |",
            "| --- | --- | --- | :---: | ---: | --- |",
        ]
    )
    for case in report.cases:
        passed = "✅" if case.metrics.get("case_pass_rate") == 1 else "❌"
        trace = f"`{case.trace_id[:8]}`" if case.trace_id else "—"
        lines.append(
            f"| `{case.case_id}` | `{case.expected_route}` | `{case.route or '—'}` | "
            f"{passed} | {case.latency_ms:,.0f} ms | {trace} |"
        )

    if report.gate.violations:
        lines.extend(["", "## Gate violations", ""])
        lines.extend(f"- {violation}" for violation in report.gate.violations)
    if report.phoenix and report.phoenix.experiment_url:
        lines.extend(["", f"[Open Phoenix experiment]({report.phoenix.experiment_url})"])
    if report.phoenix and report.phoenix.warnings:
        lines.extend(["", "## Phoenix publish warnings", ""])
        lines.extend(f"- {warning}" for warning in report.phoenix.warnings)
    lines.append("")
    return "\n".join(lines)


def load_baseline(path: Path) -> Baseline | None:
    if not path.exists():
        return None
    return Baseline.model_validate_json(path.read_text(encoding="utf-8"))


def write_baseline(path: Path, report: EvaluationReport) -> Baseline:
    baseline = Baseline(
        dataset_name=report.dataset_name,
        dataset_digest=report.dataset_digest,
        version=report.version,
        created_at=_utc_now().isoformat(),
        metrics=report.metrics,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(baseline.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return baseline


def write_report(output_dir: Path, report: EvaluationReport, policy: GatePolicy) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(
        report.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "report.md").write_text(
        render_markdown(report, policy),
        encoding="utf-8",
    )


def _phoenix_base_url(explicit: str, settings: Settings) -> str:
    value = explicit or os.environ.get("CHATDEMO_PHOENIX_URL", "")
    if value:
        return value.rstrip("/")
    endpoint = settings.phoenix_endpoint.rstrip("/")
    return endpoint.removesuffix("/v1/traces")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run ChatDemo quality evaluation")
    parser.add_argument("--dataset", type=Path, default=Path("evals/cases.yaml"))
    parser.add_argument("--baseline", type=Path, default=Path("evals/baseline.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("evals/results"))
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument(
        "--latency-p95-ceiling-ms",
        type=float,
        default=None,
        help="override only the p95 latency ceiling for this execution environment",
    )
    parser.add_argument("--version", default=os.environ.get("CHATDEMO_EVAL_VERSION", ""))
    parser.add_argument("--record-baseline", action="store_true")
    parser.add_argument("--require-baseline", action="store_true")
    publish_default = os.environ.get("CHATDEMO_EVAL_PUBLISH_PHOENIX", "").lower() in {
        "1",
        "on",
        "true",
        "yes",
    }
    parser.add_argument(
        "--phoenix",
        action=argparse.BooleanOptionalAction,
        default=publish_default,
        help="publish a Phoenix experiment (or set CHATDEMO_EVAL_PUBLISH_PHOENIX)",
    )
    parser.add_argument("--phoenix-url", default="")
    return parser


async def _run_cli(args: argparse.Namespace) -> int:
    project_root = Path(__file__).resolve().parents[2]
    dataset = load_dataset(args.dataset)
    policy = policy_with_overrides(
        dataset.policy,
        latency_p95_ceiling_ms=args.latency_p95_ceiling_ms,
    )
    settings = Settings.from_env()
    digest = dataset_digest(dataset)
    version = args.version or application_fingerprint(project_root, dataset, settings)
    run_id = f"{_utc_now():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    baseline = load_baseline(args.baseline)
    if args.require_baseline and baseline is None:
        print(f"baseline required but missing: {args.baseline}", file=sys.stderr)
        return 2

    started_at = _utc_now()
    results = await evaluate_dataset(
        dataset,
        concurrency=args.concurrency,
        run_id=run_id,
    )
    metrics = aggregate_metrics(results)
    gate = compare_metrics(metrics, policy, baseline, current_dataset_digest=digest)
    report = EvaluationReport(
        run_id=run_id,
        version=version,
        dataset_name=dataset.name,
        dataset_digest=digest,
        started_at=started_at.isoformat(),
        completed_at=_utc_now().isoformat(),
        provider={
            "model": settings.model,
            "router_model": settings.router_model,
            "skill_model": settings.skill_model,
            "base_url": settings.base_url,
            "tracing": settings.tracing,
        },
        metrics=metrics,
        baseline=baseline,
        gate=gate,
        cases=results,
    )

    if args.phoenix:
        try:
            report.phoenix = await asyncio.to_thread(
                publish_to_phoenix,
                base_url=_phoenix_base_url(args.phoenix_url, settings),
                dataset=dataset,
                report=report,
            )
        except Exception as exc:  # noqa: BLE001 - surface publishing as a gate failure
            message = f"Phoenix publish failed: {type(exc).__name__}: {exc}"
            report.gate.violations.append(message)
            report.gate.passed = False

    if args.record_baseline:
        if report.gate.passed:
            report.baseline = write_baseline(args.baseline, report)
        else:
            report.gate.violations.append("baseline not updated because the quality gate failed")
    write_report(args.output_dir, report, policy)
    print(render_markdown(report, policy))
    print(f"Reports: {args.output_dir / 'report.json'} and {args.output_dir / 'report.md'}")
    return 0 if report.gate.passed else 1


def main() -> None:
    raise SystemExit(asyncio.run(_run_cli(_parser().parse_args())))


if __name__ == "__main__":  # pragma: no cover
    main()
