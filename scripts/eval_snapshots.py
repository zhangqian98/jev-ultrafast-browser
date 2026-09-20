"""Replay saved observations through compaction and verification.

The default mode is fully offline.  ``--live`` additionally asks Jev for one
decision per case and therefore requires TYPESAFE_API_KEY and incurs API usage.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jev_ultrafast.browser import fingerprint  # noqa: E402
from jev_ultrafast.compaction import compact_actions  # noqa: E402
from jev_ultrafast.model import choose  # noqa: E402
from jev_ultrafast.policy import evaluate_policy  # noqa: E402
from jev_ultrafast.verify import verify_page  # noqa: E402


def evaluate_case(case, *, live=False):
    page = case["page"]
    compacted, stats = compact_actions(
        page.get("actions", []),
        case["goal"],
        case.get("history", []),
        focus=page.get("focus"),
        max_elements=case.get("max_elements", 180),
        max_actions=case.get("max_actions", 300),
    )
    compacted_page = {**page, "actions": compacted}
    survivor_checks = []
    for predicate in case.get("expected_survivors", []):
        survivor_checks.append(bool(verify_page(compacted_page, {"element": predicate})))
    absent_checks = []
    for predicate in case.get("expected_absent", []):
        absent_checks.append(not bool(verify_page(compacted_page, {"element": predicate})))
    expected_verify = case.get("expected_verify")
    actual_verify = verify_page(page, case.get("verify"))
    verify_ok = expected_verify is None or actual_verify is expected_verify
    expected_truncated = case.get("expected_truncated")
    truncation_ok = expected_truncated is None or stats["truncated"] is expected_truncated

    policy = case.get("policy")
    gate = evaluate_policy(**policy) if policy else None
    expected_gate = case.get("expected_gate")
    gate_ok = expected_gate is None or (gate and gate["verdict"] == expected_gate)

    stale = None
    stale_spec = case.get("stale")
    if stale_spec:
        after_page = stale_spec["after_page"]
        selected_id = stale_spec["selected_id"]
        selected_is_gone = not any(
            action.get("id") == selected_id for action in after_page.get("actions", [])
        )
        stale = fingerprint(page) != fingerprint(after_page) and selected_is_gone
    expected_stale = case.get("expected_stale")
    stale_ok = expected_stale is None or stale is expected_stale

    expected_count = len(survivor_checks)
    target_recall = (
        sum(survivor_checks) / expected_count if expected_count else None
    )
    row = {
        "name": case["name"],
        "survivors": survivor_checks,
        "absent": absent_checks,
        "target_recall": target_recall,
        "verify": actual_verify,
        "gate": gate,
        "stale": stale,
        "candidate_stats": stats,
        "passed": (
            all(survivor_checks)
            and all(absent_checks)
            and verify_ok
            and truncation_ok
            and gate_ok
            and stale_ok
        ),
    }
    if live:
        decision = choose(compacted_page, case["goal"], case.get("history", []))
        expected_operations = case.get("acceptable_operations")
        if expected_operations is None and case.get("expected_operation"):
            expected_operations = [case["expected_operation"]]
        expected_targets = case.get("acceptable_targets")
        if expected_targets is None and case.get("expected_target_contains"):
            expected_targets = [case["expected_target_contains"]]
        target = next(
            (action for action in page.get("actions", []) if action.get("id") == decision["choice"]),
            None,
        )
        operation_ok = (
            None if expected_operations is None
            else decision["operation"] in expected_operations
        )
        target_ok = None
        acceptable_ids = set()
        if expected_targets is not None:
            acceptable_ids.update(expected_targets)
            acceptable_ids.update(
                action["id"]
                for action in page.get("actions", [])
                if any(expected in str(action.get("label", "")) for expected in expected_targets)
            )
            target_ok = decision["choice"] in acceptable_ids
        ranked_targets = sorted(
            (decision.get("probabilities") or {}).items(),
            key=lambda item: item[1],
            reverse=True,
        )
        target_rank = next(
            (index for index, (target_id, _probability) in enumerate(ranked_targets, 1)
             if target_id in acceptable_ids),
            None,
        )
        live_ok = operation_ok is not False and target_ok is not False
        row["live"] = {
            "operation": decision["operation"],
            "target": (target or {}).get("label"),
            "operation_passed": operation_ok,
            "target_passed": target_ok,
            "target_rank": target_rank,
            "latency_ms": decision["latency_ms"],
            "usage": decision["usage"],
            "cost_usd": decision["cost_usd"],
            "passed": live_ok,
        }
        row["passed"] = row["passed"] and live_ok
    return row


def build_report(rows, *, live=False):
    report = {
        "mode": "live" if live else "offline",
        "passed": sum(row["passed"] for row in rows),
        "total": len(rows),
        "rows": rows,
    }
    if not live:
        return report
    live_rows = [row["live"] for row in rows]
    latencies = sorted(row["latency_ms"] for row in live_rows)
    operation_rows = [row for row in live_rows if row["operation_passed"] is not None]
    target_rows = [row for row in live_rows if row["target_passed"] is not None]
    report["live_summary"] = {
        "operation_accuracy": (
            sum(row["operation_passed"] for row in operation_rows) / len(operation_rows)
            if operation_rows else None
        ),
        "target_accuracy": (
            sum(row["target_passed"] for row in target_rows) / len(target_rows)
            if target_rows else None
        ),
        "top_3_target_accuracy": (
            sum(row["target_rank"] is not None and row["target_rank"] <= 3 for row in target_rows)
            / len(target_rows)
            if target_rows else None
        ),
        "p50_ms": latencies[len(latencies) // 2],
        "p95_ms": latencies[math.ceil(len(latencies) * 0.95) - 1],
        "input_tokens": sum(
            (row.get("usage") or {}).get(
                "input_tokens", (row.get("usage") or {}).get("inputTokens", 0)
            ) or 0
            for row in live_rows
        ),
        "cost_usd": round(sum(row.get("cost_usd", 0) or 0 for row in live_rows), 9),
    }
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default=str(ROOT / "fixtures" / "eval" / "cases.json"))
    parser.add_argument("--output")
    parser.add_argument("--live", action="store_true",
                        help="Call Jev for each case; requires TYPESAFE_API_KEY and is billable.")
    args = parser.parse_args()

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise SystemExit("Evaluation cases must be a non-empty JSON array")
    rows = [evaluate_case(case, live=args.live) for case in cases]
    report = build_report(rows, live=args.live)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    raise SystemExit(0 if report["passed"] == report["total"] else 1)


if __name__ == "__main__":
    main()
