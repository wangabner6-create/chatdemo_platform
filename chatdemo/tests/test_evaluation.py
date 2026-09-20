from __future__ import annotations

from datetime import UTC, datetime

import pytest

from chatdemo.contracts import Response, ResponseMeta, Source
from chatdemo.evaluation import (
    Baseline,
    EvalCase,
    EvalDataset,
    GatePolicy,
    aggregate_metrics,
    compare_metrics,
    evaluate_dataset,
    score_response,
)


def _case() -> EvalCase:
    return EvalCase(
        id="docs-state",
        query="How is state stored?",
        expected_route="docs-assistant",
        expected_sources=["repo://SystemDesign.md"],
        required_terms=["ChatSafeSession", "session_id"],
    )


def test_code_evaluators_score_route_retrieval_and_citations() -> None:
    response = Response(
        answer="ChatSafeSession is keyed by session_id [2].",
        sources=[
            Source(url="repo://README.md"),
            Source(url="repo://SystemDesign.md"),
        ],
        meta=ResponseMeta(route="docs-assistant"),
    )

    metrics = score_response(_case(), response)

    assert metrics == {
        "route_accuracy": 1.0,
        "state_accuracy": 1.0,
        "answer_present_rate": 1.0,
        "retrieval_hit_rate": 1.0,
        "citation_validity": 1.0,
        "expected_source_citation_rate": 1.0,
        "term_coverage": 1.0,
        "error_rate": 0.0,
        "case_pass_rate": 1.0,
    }


def test_invalid_or_unmatched_citation_fails_case() -> None:
    response = Response(
        answer="ChatSafeSession uses session_id [3].",
        sources=[Source(url="repo://SystemDesign.md")],
        meta=ResponseMeta(route="docs-assistant"),
    )

    metrics = score_response(_case(), response)

    assert metrics["retrieval_hit_rate"] == 1.0
    assert metrics["citation_validity"] == 0.0
    assert metrics["expected_source_citation_rate"] == 0.0
    assert metrics["case_pass_rate"] == 0.0


def test_required_concepts_accept_lexical_alternatives() -> None:
    case = _case().model_copy(
        update={
            "required_terms": [
                ["router", "routing", "路由"],
                ["specialist", "专家"],
            ]
        }
    )
    response = Response(
        answer="Routing selects a specialist [1].",
        sources=[Source(url="repo://SystemDesign.md")],
        meta=ResponseMeta(route="docs-assistant"),
    )

    assert score_response(case, response)["term_coverage"] == 1.0


@pytest.mark.asyncio
async def test_dataset_runner_isolates_cases_and_aggregates() -> None:
    sessions: list[str] = []

    async def fake_agent(query: str, session_id: str, user) -> Response:
        sessions.append(session_id)
        return Response(
            answer="ChatSafeSession uses session_id [1].",
            sources=[Source(url="repo://SystemDesign.md")],
            meta=ResponseMeta(
                route="docs-assistant",
                model="test-model",
                trace_id="a" * 32,
            ),
        )

    dataset = EvalDataset(name="test", cases=[_case(), _case().model_copy(update={"id": "b"})])
    results = await evaluate_dataset(dataset, agent=fake_agent, concurrency=2, run_id="run")
    metrics = aggregate_metrics(results)

    assert len(set(sessions)) == 2
    assert metrics["route_accuracy"] == 1.0
    assert metrics["case_pass_rate"] == 1.0
    assert metrics["error_rate"] == 0.0
    assert metrics["latency_p95_ms"] >= 0


def test_gate_enforces_floor_and_baseline_regression() -> None:
    digest = "d" * 64
    baseline = Baseline(
        dataset_name="test",
        dataset_digest=digest,
        version="previous",
        created_at=datetime.now(UTC).isoformat(),
        metrics={"route_accuracy": 1.0, "latency_p95_ms": 1000.0},
    )
    policy = GatePolicy(
        minimum={"route_accuracy": 0.8},
        max_drop_from_baseline={"route_accuracy": 0.05},
        max_relative_increase={"latency_p95_ms": 0.2},
    )

    gate = compare_metrics(
        {"route_accuracy": 0.9, "latency_p95_ms": 1300.0},
        policy,
        baseline,
        current_dataset_digest=digest,
    )

    assert gate.passed is False
    assert any("route_accuracy" in violation for violation in gate.violations)
    assert any("latency_p95_ms" in violation for violation in gate.violations)


def test_gate_rejects_stale_dataset_baseline() -> None:
    baseline = Baseline(
        dataset_name="test",
        dataset_digest="old",
        version="previous",
        created_at=datetime.now(UTC).isoformat(),
        metrics={},
    )

    gate = compare_metrics({}, GatePolicy(), baseline, current_dataset_digest="new")

    assert gate.passed is False
    assert gate.violations == [
        "dataset changed: record a reviewed baseline for the new dataset version"
    ]
