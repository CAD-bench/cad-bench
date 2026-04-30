from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harnesses.costs import TokenUsage, estimate_token_cost_usd, pricing_for

DIFFICULTIES = ("easy", "medium", "hard", "insane")
TABLES = ("standalone_model", "agent")


def _run_id_from_report_path(path: Path) -> str:
    if path.name == "report.json":
        return path.parent.name
    return path.stem


def _is_complete_report(report: dict[str, Any]) -> bool:
    rows = report.get("rows")
    summary = report.get("summary")
    if not isinstance(rows, list) or len(rows) != 17:
        return False
    if report.get("benchmark_task_count") != 17:
        return False
    if not isinstance(summary, dict):
        return False
    by_difficulty = summary.get("by_difficulty")
    return isinstance(by_difficulty, dict) and set(DIFFICULTIES).issubset(by_difficulty)


def _usage_cost(rows: list[dict[str, Any]]) -> tuple[float | None, bool]:
    total = 0.0
    saw_cost = False
    precise = True
    for row in rows:
        usage = row.get("usage")
        if not isinstance(usage, dict):
            precise = False
            continue
        cost = usage.get("estimated_cost_usd")
        if cost is None:
            precise = False
            continue
        total += float(cost)
        saw_cost = True
        precise = precise and bool(usage.get("cost_is_precise", False))
    if not saw_cost:
        return None, False
    if total <= 0.0:
        return None, False
    return total, precise


def _listed_cost(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    cost = float(value)
    if cost <= 0.0:
        return None
    return cost


def _table_name(harness: dict[str, Any]) -> str:
    strategy = str(harness.get("strategy", ""))
    if strategy == "agent_step":
        return "agent"
    return "standalone_model"


def _task_row(row: dict[str, Any]) -> dict[str, Any]:
    metrics = row.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    return {
        "task_id": row.get("task_id"),
        "difficulty": row.get("difficulty"),
        "elapsed_s": row.get("elapsed_s"),
        "attempt_count": row.get("attempt_count"),
        "build_success": metrics.get("build_success"),
        "overall_score": metrics.get("overall_score"),
        "task_score": metrics.get("task_score"),
        "reward": metrics.get("reward"),
        "error": row.get("error") or row.get("export_error") or row.get("score_error"),
    }


def _result_record(path: Path, report: dict[str, Any]) -> dict[str, Any]:
    harness = report["harness"]
    rows = report["rows"]
    summary = report["summary"]
    by_difficulty = summary["by_difficulty"]
    cost, cost_is_precise = _usage_cost(rows)
    return {
        "run_id": _run_id_from_report_path(path),
        "table": _table_name(harness),
        "harness_id": harness.get("id"),
        "provider": harness.get("provider"),
        "strategy": harness.get("strategy"),
        "model": harness.get("model"),
        "access": harness.get("access"),
        "reasoning_effort": harness.get("reasoning_effort") or "",
        "benchmark_score": summary.get("benchmark_score"),
        "by_difficulty": {key: by_difficulty.get(key) for key in DIFFICULTIES},
        "elapsed_s": sum(float(row.get("elapsed_s") or 0.0) for row in rows),
        "estimated_cost_usd": cost,
        "cost_is_precise": cost_is_precise,
        "task_count": len(rows),
        "tasks": [_task_row(row) for row in rows],
    }


def _cost_for_website_row(row: dict[str, Any]) -> tuple[float | None, bool, str]:
    existing = row.get("estimated_cost_usd")
    if isinstance(existing, (int, float)):
        cost = _listed_cost(existing)
        return (
            cost,
            bool(row.get("cost_is_precise", False)) if cost is not None else False,
            "website_payload" if cost is not None else "not_listed",
        )
    usage = TokenUsage(
        input_tokens=int(row.get("input_tokens") or 0),
        cached_input_tokens=int(row.get("cached_input_tokens") or 0),
        output_tokens=int(row.get("output_tokens") or 0),
        total_tokens=int(row.get("total_tokens") or 0),
    )
    pricing = pricing_for(str(row.get("provider") or ""), str(row.get("model") or ""))
    if pricing is None:
        return None, False, "unknown_model_pricing"
    cost, pricing = estimate_token_cost_usd(
        str(row.get("provider") or ""),
        str(row.get("model") or ""),
        usage,
    )
    listed_cost = _listed_cost(cost)
    return (
        listed_cost,
        bool(pricing and pricing.precise) if listed_cost is not None else False,
        pricing.source if listed_cost is not None and pricing else "not_listed",
    )


def _website_table_name(row: dict[str, Any]) -> str:
    agent = str(row.get("agent") or "").strip().lower()
    strategy = str(row.get("strategy") or "").strip().lower()
    if agent == "none" or strategy == "one_shot_code":
        return "standalone_model"
    return "agent"


def _result_record_from_website_row(row: dict[str, Any]) -> dict[str, Any]:
    cost, precise, cost_source = _cost_for_website_row(row)
    return {
        "run_id": row.get("run_name"),
        "table": _website_table_name(row),
        "harness_id": row.get("harness_id"),
        "company": row.get("company"),
        "provider": row.get("provider"),
        "agent": row.get("agent"),
        "strategy": row.get("strategy"),
        "model": row.get("model"),
        "access": row.get("access"),
        "web_access": row.get("web_access"),
        "reasoning_effort": row.get("reasoning_effort") or "",
        "timestamp_utc": row.get("timestamp_utc"),
        "benchmark_score": row.get("overall"),
        "by_difficulty": {key: row.get(key) for key in DIFFICULTIES},
        "elapsed_s": row.get("elapsed_s"),
        "estimated_cost_usd": cost,
        "cost_is_precise": precise,
        "cost_source": cost_source,
        "input_tokens": row.get("input_tokens"),
        "cached_input_tokens": row.get("cached_input_tokens"),
        "output_tokens": row.get("output_tokens"),
        "total_tokens": row.get("total_tokens"),
        "task_count": row.get("task_count"),
        "expected_task_count": row.get("expected_task_count"),
        "missing_task_count": row.get("missing_task_count"),
    }


def _extract_website_payload(index_html: str) -> list[dict[str, Any]]:
    match = re.search(
        r'<script id="payload" type="application/json">(.*?)</script>',
        index_html,
        re.DOTALL,
    )
    if match is None:
        raise ValueError("website HTML does not contain the CAD-bench payload script")
    rows = json.loads(match.group(1))
    if not isinstance(rows, list):
        raise TypeError("website payload must be a list")
    return [row for row in rows if isinstance(row, dict)]


def build_payload_from_website(
    index_html_path: Path,
    *,
    task_revision: str,
    website_commit: str = "",
) -> dict[str, Any]:
    website_rows = _extract_website_payload(index_html_path.read_text(encoding="utf-8"))
    complete = [
        _result_record_from_website_row(row)
        for row in website_rows
        if int(row.get("task_count") or 0) == 17
        and int(row.get("expected_task_count") or 0) == 17
        and int(row.get("missing_task_count") or 0) == 0
    ]
    table_order = {table: index for index, table in enumerate(TABLES)}
    complete.sort(
        key=lambda item: (
            table_order.get(str(item["table"]), 99),
            -float(item["benchmark_score"]),
            str(item["run_id"]),
        )
    )
    return {
        "schema_version": "2.0",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "source": "CAD-bench website rendered payload",
        "source_url": "https://cad-bench.github.io/",
        "source_repo": "CAD-bench/cad-bench.github.io",
        "source_commit": website_commit,
        "task_dataset_repo_id": "CAD-bench/cad-bench-ed-2026-anonymous-tasks",
        "task_dataset_revision": task_revision,
        "selection_policy": (
            "All complete 17-task rows displayed by the CAD-bench website payload "
            "are included. Rows with agent='none' are reported as standalone "
            "model runs. Rows with an agent value other than 'none' are reported "
            "as agent runs."
        ),
        "pricing_policy": (
            "Costs are copied from the website payload when present. Missing "
            "or zero-valued costs are reported as not listed. Missing "
            "standalone GPT-5.3 Codex Spark costs are estimated from current "
            "public OpenAI token pricing. GPT-5.3 Codex Spark does not have a "
            "separate public price in the checked OpenAI pricing table, so the "
            "GPT-5.3 Codex token price is applied and marked non-precise. "
            "Qwen 3.6 costs in the website payload use current Vercel AI "
            "Gateway token pricing and are marked non-precise when the "
            "gateway publishes tiered token prices."
        ),
        "tables": {
            "standalone_model": "Complete standalone model rows with agent='none'.",
            "agent": "Complete agent rows with agent!='none'.",
        },
        "complete_run_count": len(complete),
        "complete_runs_by_table": {
            table: sum(1 for item in complete if item["table"] == table)
            for table in TABLES
        },
        "results": complete,
        "excluded_runs": [],
    }


def build_payload(report_paths: list[Path], *, task_revision: str) -> dict[str, Any]:
    complete: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for path in sorted(report_paths):
        report = json.loads(path.read_text(encoding="utf-8"))
        if _is_complete_report(report):
            complete.append(_result_record(path, report))
        else:
            harness = report.get("harness") if isinstance(report, dict) else {}
            if not isinstance(harness, dict):
                harness = {}
            excluded.append(
                {
                    "run_id": _run_id_from_report_path(path),
                    "harness_id": harness.get("id"),
                    "provider": harness.get("provider"),
                    "row_count": len(report.get("rows", []))
                    if isinstance(report.get("rows"), list)
                    else 0,
                    "reason": "not a complete 17-task report",
                }
            )
    table_order = {table: index for index, table in enumerate(TABLES)}
    complete.sort(
        key=lambda item: (
            table_order.get(str(item["table"]), 99),
            -float(item["benchmark_score"]),
            str(item["run_id"]),
        )
    )
    return {
        "schema_version": "1.1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "task_dataset_repo_id": "CAD-bench/cad-bench-ed-2026-anonymous-tasks",
        "task_dataset_revision": task_revision,
        "selection_policy": (
            "All complete 17-task reports available in the reviewer report mirror "
            "are included. Partial runs and provider/infrastructure failures are "
            "listed only in excluded_runs and are not treated as model evaluations."
        ),
        "tables": {
            "standalone_model": "Complete standalone model rows.",
            "agent": "Complete agent rows.",
        },
        "complete_run_count": len(complete),
        "complete_runs_by_table": {
            table: sum(1 for item in complete if item["table"] == table)
            for table in TABLES
        },
        "results": complete,
        "excluded_runs": excluded,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export reviewer-facing CAD-bench results from report.json files."
    )
    parser.add_argument(
        "--reports-root",
        type=Path,
        default=Path("outputs/review_reports"),
        help="Directory containing run subdirectories with report.json files.",
    )
    parser.add_argument(
        "--website-index-html",
        type=Path,
        default=None,
        help="Optional CAD-bench website index.html. When provided, export the rendered website result payload.",
    )
    parser.add_argument(
        "--website-commit",
        default="",
        help="Optional CAD-bench website commit SHA corresponding to --website-index-html.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("metadata/cad-bench-reported-results.json"),
        help="Output JSON path.",
    )
    parser.add_argument(
        "--task-revision",
        default="fbcba766afcb4d4469d0297ceb819db2527c707d",
        help="Pinned task dataset revision used by the paper.",
    )
    args = parser.parse_args()

    if args.website_index_html is not None:
        payload = build_payload_from_website(
            args.website_index_html,
            task_revision=args.task_revision,
            website_commit=args.website_commit,
        )
    else:
        report_paths = sorted(args.reports_root.glob("*/report.json"))
        if not report_paths:
            raise SystemExit(f"no report.json files found under {args.reports_root}")
        payload = build_payload(report_paths, task_revision=args.task_revision)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        f"wrote {args.output} with {payload['complete_run_count']} complete runs "
        f"and {len(payload['excluded_runs'])} excluded incomplete runs"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
