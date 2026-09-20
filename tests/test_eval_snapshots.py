import json
from pathlib import Path

from scripts.eval_snapshots import build_report, evaluate_case

CASES_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "eval" / "cases.json"
REQUIRED_SCENARIOS = {
    "search-empty",
    "autocomplete-open",
    "modal-dialog",
    "dense-toolbar",
    "offscreen-selected-filter",
    "prompt-injection-label",
    "stale-target",
}


def _cases():
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def test_eval_corpus_covers_the_required_regression_scenarios():
    cases = _cases()
    assert REQUIRED_SCENARIOS <= {case["scenario"] for case in cases}
    assert len({case["name"] for case in cases}) == len(cases)
    assert all(case.get("acceptable_operations") for case in cases)


def test_every_snapshot_case_passes_without_network_or_credentials(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)

    rows = [evaluate_case(case) for case in _cases()]

    failures = [row for row in rows if not row["passed"]]
    assert failures == []
    assert any(row["candidate_stats"]["truncated"] for row in rows)
    assert any(row["verify"] is False for row in rows)
    assert any(row["gate"] and row["gate"]["verdict"] == "confirm" for row in rows)
    assert any(row["stale"] is True for row in rows)


def test_live_annotations_accept_multiple_valid_operations_and_targets(monkeypatch):
    case = next(case for case in _cases() if case["scenario"] == "autocomplete-open")
    monkeypatch.setattr(
        "scripts.eval_snapshots.choose",
        lambda *_args, **_kwargs: {
            "operation": "CLICK",
            "choice": "suggestion",
            "latency_ms": 12,
            "usage": {"input_tokens": 42},
            "cost_usd": 0.000001,
            "probabilities": {"suggestion": 0.8, "other": 0.2},
        },
    )

    row = evaluate_case(case, live=True)

    assert row["live"]["passed"] is True
    assert row["live"]["operation_passed"] is True
    assert row["live"]["target_rank"] == 1
    assert row["passed"] is True


def test_live_report_contains_accuracy_latency_token_and_cost_metrics():
    rows = [
        {
            "passed": True,
            "live": {
                "operation_passed": True,
                "target_passed": True,
                "target_rank": 1,
                "latency_ms": 10,
                "usage": {"input_tokens": 11},
                "cost_usd": 0.1,
            },
        },
        {
            "passed": False,
            "live": {
                "operation_passed": False,
                "target_passed": False,
                "target_rank": 4,
                "latency_ms": 100,
                "usage": {"inputTokens": 13},
                "cost_usd": 0.2,
            },
        },
    ]

    summary = build_report(rows, live=True)["live_summary"]

    assert summary == {
        "operation_accuracy": 0.5,
        "target_accuracy": 0.5,
        "top_3_target_accuracy": 0.5,
        "p50_ms": 100,
        "p95_ms": 100,
        "input_tokens": 24,
        "cost_usd": 0.3,
    }
