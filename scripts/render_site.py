import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from bench.config import (
    BUILD_SUCCESS_REWARD_WEIGHT,
    OVERALL_SCORE_REWARD_WEIGHT,
    clamp_unit_interval,
    summarize_difficulty_aggregates,
)
from bench.runner import TASK_DEFS, TASK_IDS
from bench.provenance import resolve_hf_token
from harnesses.costs import usage_with_cost
from huggingface_hub import HfApi, hf_hub_download

REPO_ROOT = Path(__file__).resolve().parent.parent
WEBSITE_HTML = Path("index.html")
APPROVED_RUNS_JSON = Path("approved_runs.json")
TASK_SCORE_FIELDS = {
    "reward",
    "overall_score",
    "task_score",
    "build_success",
    "submission_exists",
}
FULL_BENCHMARK_TASK_COUNT = len(TASK_IDS)
TASK_DIFFICULTIES = {
    task_id: str(task.difficulty) for task_id, task in TASK_DEFS.items()
}


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _optional_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _listed_cost(value: Any) -> float | None:
    cost = _optional_number(value)
    if cost is None or cost <= 0.0:
        return None
    return cost


def _first_number(mapping: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _optional_number(mapping.get(key))
        if value is not None:
            return value
    return None


def _canonical_metrics(values: Any) -> dict[str, Any]:
    if not isinstance(values, dict):
        values = {}
    build_success = _first_number(values, "build_success")
    if build_success is None:
        build_success = 0.0
    submission_exists = _first_number(values, "submission_exists")
    if submission_exists is None:
        submission_exists = 1.0 if build_success > 0.0 else 0.0
    overall_score = _first_number(values, "overall_score")
    if overall_score is None:
        task_score_value = _first_number(values, "task_score")
        if task_score_value is not None:
            overall_score = task_score_value
    if overall_score is None:
        overall_score = _optional_number(values.get("reward")) or 0.0
    task_score = _first_number(values, "task_score")
    if task_score is None:
        task_score = overall_score
    reward = BUILD_SUCCESS_REWARD_WEIGHT * build_success + OVERALL_SCORE_REWARD_WEIGHT * overall_score
    return {
        "submission_exists": clamp_unit_interval(submission_exists),
        "build_success": clamp_unit_interval(build_success),
        "reward": clamp_unit_interval(reward),
        "overall_score": clamp_unit_interval(overall_score),
        "task_score": clamp_unit_interval(task_score),
        "score_error": _text(values.get("score_error")),
    }


def _canonical_task_row(row: dict[str, Any]) -> dict[str, Any]:
    metrics = _canonical_metrics(row)
    score_values = {
        "reward": round(_number(metrics.get("reward")), 6),
        "overall_score": round(_number(metrics.get("overall_score")), 6),
        "task_score": round(_number(metrics.get("task_score")), 6),
        "build_success": round(_number(metrics.get("build_success")), 6),
        "submission_exists": round(_number(metrics.get("submission_exists")), 6),
    }
    canonical: dict[str, Any] = {}
    inserted_scores = False
    for key, value in row.items():
        if (
            key in {"run_dir", "task_dir"}
            or key.endswith("_path")
            or key.endswith("_href")
        ):
            continue
        if key in TASK_SCORE_FIELDS:
            if key == "reward" and not inserted_scores:
                canonical.update(score_values)
                inserted_scores = True
            continue
        if key == "score_error" and not inserted_scores:
            canonical.update(score_values)
            inserted_scores = True
        canonical[key] = value
    if not inserted_scores:
        canonical.update(score_values)
    if not _text(canonical.get("status")):
        canonical["status"] = _status_for_metrics(
            error=_text(canonical.get("error")),
            metrics=metrics,
        )
    return canonical


def _text(value: Any) -> str:
    return str(value or "").strip()


def _status_for_metrics(*, error: str, metrics: dict[str, Any]) -> str:
    if _text(error):
        return "agent_error"
    if _number(metrics.get("submission_exists")) < 0.5:
        return "missing_submission"
    if _number(metrics.get("build_success")) < 0.5:
        return "build_failed"
    if _number(metrics.get("reward")) >= 0.999:
        return "pass"
    return "scored"


def _status_for_row(row: dict[str, Any]) -> str:
    metrics = row.get("metrics", {})
    if not isinstance(metrics, dict):
        return "unknown"
    return _status_for_metrics(
        error=_text(row.get("error")),
        metrics=_canonical_metrics(metrics),
    )


def _row_is_agent_error(row: dict[str, Any]) -> bool:
    return _status_for_metrics(
        error=_text(row.get("error")),
        metrics=_canonical_metrics(row.get("metrics", {})),
    ) == "agent_error"


def _exclude_published_row(*, error: str) -> bool:
    del error
    return False


def _agent_name(provider: str, strategy: str = "") -> str:
    provider_name = _text(provider).lower()
    strategy_name = _text(strategy).lower()
    if strategy_name != "agent_step":
        return "none"
    if provider_name in {"openai", "openai_codex"}:
        return "none"
    if provider_name in {"pi", "codex"}:
        return provider_name
    return provider_name or "none"


def _company_name(provider: str) -> str:
    provider_name = _text(provider).lower()
    if provider_name in {"openai", "openai_codex", "pi", "codex"}:
        return "OpenAI"
    return provider_name.title() if provider_name else "Unknown"


def _has_web_access(access: str) -> bool:
    return _text(access).lower() in {"web", "web_ci"}


def _display_model_name(model: str, reasoning_effort: str) -> str:
    model_text = _text(model)
    effort_text = _text(reasoning_effort)
    if model_text and effort_text:
        return f"{model_text} {effort_text}"
    return model_text


@lru_cache(maxsize=8192)
def _load_usage_payload(path_text: str) -> dict[str, Any] | None:
    if not path_text:
        return None
    path = Path(path_text)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"usage payload must be an object: {path}")
    return payload


def _usage_from_task_row(row: dict[str, Any]) -> dict[str, Any]:
    usage_path = _text(row.get("usage_path"))
    payload = _load_usage_payload(usage_path)
    if payload is not None:
        return usage_with_cost(
            _text(row.get("provider")),
            _text(row.get("model")),
            payload,
            access=_text(row.get("access")),
        )
    return {
        "input_tokens": int(_number(row.get("input_tokens"))),
        "cached_input_tokens": int(_number(row.get("cached_input_tokens"))),
        "output_tokens": int(_number(row.get("output_tokens"))),
        "total_tokens": int(_number(row.get("total_tokens"))),
        "estimated_cost_usd": _optional_number(row.get("estimated_cost_usd")),
        "cost_is_precise": bool(row.get("cost_is_precise")),
        "cost_source": _text(row.get("cost_source")),
    }


def _summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "benchmark_score": 0.0,
            "by_difficulty": {},
        }
    canonical_rows = [
        {
            "difficulty": _text(row.get("difficulty")),
            "metrics": _canonical_metrics(row.get("metrics", {})),
        }
        for row in rows
    ]
    by_difficulty, _, benchmark_score = summarize_difficulty_aggregates(canonical_rows)
    return {
        "benchmark_score": benchmark_score,
        "by_difficulty": by_difficulty,
    }


def _zero_score_summary_row(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "difficulty": TASK_DIFFICULTIES[task_id],
        "metrics": {
            "reward": 0.0,
            "overall_score": 0.0,
            "task_score": 0.0,
            "build_success": 0.0,
            "submission_exists": 0.0,
        },
        "usage": {},
    }


def _score_rows_with_missing_benchmark_tasks_as_zero(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    present_task_ids = {_text(row.get("task_id")) for row in rows}
    if not any(task_id in TASK_DIFFICULTIES for task_id in present_task_ids):
        return rows
    complete_rows = list(rows)
    complete_rows.extend(
        _zero_score_summary_row(task_id)
        for task_id in TASK_IDS
        if task_id not in present_task_ids
    )
    return complete_rows


def _build_payload_from_task_rows(task_rows: list[dict[str, Any]], source_dir: str) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    published_task_rows: list[dict[str, Any]] = []
    for row in task_rows:
        row = _canonical_task_row(row)
        if _exclude_published_row(
            error=_text(row.get("error")),
        ):
            continue
        row_usage = _usage_from_task_row(row)
        row["input_tokens"] = int(_number(row_usage.get("input_tokens")))
        row["cached_input_tokens"] = int(_number(row_usage.get("cached_input_tokens")))
        row["output_tokens"] = int(_number(row_usage.get("output_tokens")))
        row["total_tokens"] = int(_number(row_usage.get("total_tokens")))
        row["estimated_cost_usd"] = _listed_cost(row_usage.get("estimated_cost_usd"))
        row["cost_is_precise"] = bool(row_usage.get("cost_is_precise"))
        row["cost_source"] = _text(row_usage.get("cost_source"))
        published_task_rows.append(row)
        harness_id = _text(row.get("harness_id"))
        if harness_id:
            grouped.setdefault(harness_id, []).append(row)

    harness_rows: list[dict[str, Any]] = []
    for harness_id, rows in grouped.items():
        latest_by_task: dict[str, dict[str, Any]] = {}
        for row in rows:
            task_id = _text(row.get("task_id"))
            if not task_id:
                continue
            current = latest_by_task.get(task_id)
            row_key = (_text(row.get("timestamp_utc")), _text(row.get("run_name")))
            if current is None:
                latest_by_task[task_id] = row
                continue
            current_key = (_text(current.get("timestamp_utc")), _text(current.get("run_name")))
            if row_key > current_key:
                latest_by_task[task_id] = row
        summary_rows = sorted(latest_by_task.values(), key=lambda item: int(_number(item.get("task_index"))))
        latest_row = max(summary_rows, key=lambda item: (_text(item.get("timestamp_utc")), _text(item.get("run_name"))))
        scoring_rows = _score_rows_with_missing_benchmark_tasks_as_zero(summary_rows)
        summary_input = [
            {
                "difficulty": _text(row.get("difficulty")),
                "metrics": {
                    "reward": _number(row.get("reward")),
                    "overall_score": _number(row.get("overall_score")),
                    "task_score": _number(row.get("task_score")),
                    "build_success": _number(row.get("build_success")),
                    "submission_exists": _number(row.get("submission_exists")),
                },
                "usage": _usage_from_task_row(row),
            }
            for row in scoring_rows
        ]
        summary = _summarize_rows(summary_input)
        by_difficulty = summary.get("by_difficulty", {})
        if not isinstance(by_difficulty, dict):
            by_difficulty = {}
        overall = _number(summary.get("benchmark_score"))
        total_input_tokens = sum(
            int(_number(row.get("input_tokens"))) for row in summary_rows
        )
        total_cached_input_tokens = sum(
            int(_number(row.get("cached_input_tokens"))) for row in summary_rows
        )
        total_output_tokens = sum(
            int(_number(row.get("output_tokens"))) for row in summary_rows
        )
        total_tokens = sum(int(_number(row.get("total_tokens"))) for row in summary_rows)
        total_elapsed_s = round(
            sum(_number(row.get("elapsed_s")) for row in summary_rows), 3
        )
        cost_values = [
            _optional_number(row.get("estimated_cost_usd")) for row in summary_rows
        ]
        total_cost = (
            round(sum(value for value in cost_values if value is not None), 6)
            if all(value is not None for value in cost_values)
            else None
        )
        total_cost = _listed_cost(total_cost)
        cost_is_precise = all(bool(row.get("cost_is_precise")) for row in summary_rows)
        if overall > 0.0:
            harness_rows.append(
                {
                    "harness_id": harness_id,
                    "company": _text(latest_row.get("company"))
                    or _company_name(_text(latest_row.get("provider"))),
                    "provider": _text(latest_row.get("provider")),
                    "agent": _agent_name(_text(latest_row.get("provider")), _text(latest_row.get("strategy"))),
                    "model": _display_model_name(_text(latest_row.get("model")), _text(latest_row.get("reasoning_effort"))),
                    "access": _text(latest_row.get("access")),
                    "web_access": bool(latest_row.get("web_access")) if "web_access" in latest_row else _has_web_access(_text(latest_row.get("access"))),
                    "reasoning_effort": _text(latest_row.get("reasoning_effort")),
                    "strategy": _text(latest_row.get("strategy")),
                    "timestamp_utc": _text(latest_row.get("timestamp_utc")),
                    "run_name": _text(latest_row.get("run_name")),
                    "task_count": len(summary_rows),
                    "expected_task_count": len(scoring_rows),
                    "missing_task_count": max(0, len(scoring_rows) - len(summary_rows)),
                    "overall": overall,
                    "easy": _number(by_difficulty.get("easy")),
                    "medium": _number(by_difficulty.get("medium")),
                    "hard": _number(by_difficulty.get("hard")),
                    "insane": _number(by_difficulty.get("insane")),
                    "input_tokens": total_input_tokens,
                    "cached_input_tokens": total_cached_input_tokens,
                    "output_tokens": total_output_tokens,
                    "total_tokens": total_tokens,
                    "elapsed_s": total_elapsed_s,
                    "estimated_cost_usd": total_cost,
                    "cost_is_precise": cost_is_precise,
                }
            )

    harness_rows.sort(key=lambda item: (-item["overall"], item["harness_id"]))
    published_task_rows.sort(key=lambda item: (_text(item.get("provider")), _text(item.get("model")), _text(item.get("access")), _text(item.get("reasoning_effort")), int(_number(item.get("task_index"))), _text(item.get("timestamp_utc")), _text(item.get("run_name"))))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_dir": source_dir,
        "total_harnesses": len(harness_rows),
        "total_task_runs": len(published_task_rows),
        "harnesses": harness_rows,
        "task_runs": published_task_rows,
    }


def _html_template(payload: dict[str, Any]) -> str:
    table_payload = [
        row for row in payload.get("harnesses", []) if isinstance(row, dict)
    ]
    table_payload.sort(
        key=lambda item: (-_number(item.get("overall")), _text(item.get("harness_id")))
    )
    payload_json = json.dumps(
        table_payload, separators=(",", ":"), ensure_ascii=True
    ).replace("</script>", "<\\/script>")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CAD Bench Results</title>
  <meta name="color-scheme" content="light dark">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
  <link href="https://unpkg.com/tabulator-tables@6.3.1/dist/css/tabulator.min.css" rel="stylesheet">
  <style>
    :root {{
      color-scheme: light dark;
      font-family: "Inter", sans-serif;
      font-size: 18px;
      --table-scale: 1;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: inherit;
      background: #fffaf6;
      color: #1f1712;
    }}
    .page {{
      width: 100%;
      padding: 16px 20px 24px;
    }}
    .controls {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px 20px;
      align-items: center;
      margin-bottom: 16px;
    }}
    .controls-title {{
      font-size: 0.95rem;
      font-weight: 600;
      white-space: nowrap;
    }}
    .tabulator .tabulator-header .tabulator-col .tabulator-col-content,
    .tabulator .tabulator-header .tabulator-col .tabulator-col-content .tabulator-col-title {{
      overflow: visible !important;
      text-overflow: clip !important;
    }}
    .tabulator .tabulator-header .tabulator-col .tabulator-col-content .tabulator-col-title {{
      width: 100%;
      white-space: nowrap !important;
      line-height: 1.1;
    }}
    .toggle-list {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px 18px;
      align-items: center;
    }}
    .toggle {{
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      white-space: nowrap;
      cursor: pointer;
    }}
    .toggle input {{
      inline-size: 1rem;
      block-size: 1rem;
      margin: 0;
    }}
    .table-shell {{
      width: 100%;
      overflow-x: auto;
      overscroll-behavior-x: contain;
      -webkit-overflow-scrolling: touch;
      padding-right: 40px;
    }}
    #harness-table {{
      width: max-content;
      min-width: 0;
    }}
    .status {{
      margin-bottom: 12px;
      font-size: 0.95rem;
      color: #5d4032;
    }}
    .tabulator {{
      width: max-content;
      font-family: inherit;
      font-size: calc(0.95rem * var(--table-scale));
      border: 1px solid #ddd6ce;
      background: #fffdf9;
      color: inherit;
    }}
    .tabulator .tabulator-header {{
      background: #f5e6dc;
      border-bottom: 1px solid #d8b8a2;
      color: inherit;
    }}
    .tabulator .tabulator-header .tabulator-col {{
      font-size: calc(0.95rem * var(--table-scale));
      font-weight: 600;
      background: #f5e6dc;
      border-right: 0 !important;
      transition: background-color 120ms ease;
    }}
    .tabulator .tabulator-header .tabulator-col:hover {{
      background: #eed8c9;
    }}
    .tabulator .tabulator-header .tabulator-col .tabulator-header-filter {{
      margin-top: calc(0.35rem * var(--table-scale));
    }}
    .tabulator .tabulator-header .tabulator-col .tabulator-header-filter input,
    .tabulator .tabulator-header .tabulator-col .tabulator-header-filter select {{
      width: 100%;
      padding: calc(0.24rem * var(--table-scale)) calc(0.5rem * var(--table-scale));
      border: 1px solid #d8b8a2;
      border-radius: 0;
      background: #fffdf9;
      color: inherit;
      font: inherit;
    }}
    .tabulator .tabulator-row .tabulator-cell {{
      padding: calc(0.7rem * var(--table-scale)) calc(0.8rem * var(--table-scale));
      border-right: 0 !important;
      white-space: nowrap !important;
      overflow: visible !important;
      text-overflow: clip !important;
    }}
    .tabulator .tabulator-row {{
      background: #fffdf9;
      border-bottom: 1px solid #eee3da;
    }}
    .tabulator .tabulator-row:nth-child(even) {{
      background: #fff8ef;
    }}
    .tabulator .tabulator-row:hover {{
      background: #f4e3d6;
    }}
    @media (max-width: 640px) {{
      :root {{
        font-size: 16px;
      }}
      .page {{
        padding: 8px 8px 16px;
      }}
      .controls {{
        gap: 8px 12px;
        margin-bottom: 10px;
      }}
      .toggle-list {{
        gap: 8px 12px;
      }}
      .toggle {{
        gap: 0.35rem;
        font-size: 0.85rem;
      }}
      .toggle input {{
        inline-size: 0.9rem;
        block-size: 0.9rem;
      }}
      .tabulator {{
        width: 100%;
        font-size: calc(0.82rem * var(--table-scale));
      }}
      .tabulator .tabulator-header .tabulator-col {{
        font-size: calc(0.8rem * var(--table-scale));
      }}
      .tabulator .tabulator-header .tabulator-col .tabulator-col-content {{
        padding: 4px 1px 4px 3px;
      }}
      .tabulator .tabulator-header .tabulator-col .tabulator-header-filter input,
      .tabulator .tabulator-header .tabulator-col .tabulator-header-filter select {{
        padding: calc(0.16rem * var(--table-scale)) calc(0.18rem * var(--table-scale));
      }}
      .tabulator .tabulator-tableholder {{
        overflow-x: auto !important;
        overscroll-behavior-x: contain;
        touch-action: pan-x pan-y;
      }}
      .tabulator .tabulator-header .tabulator-col .tabulator-col-content .tabulator-col-sorter,
      .tabulator .tabulator-header .tabulator-col .tabulator-col-content .tabulator-col-sorter .tabulator-arrow {{
        display: none !important;
      }}
      .tabulator .tabulator-row .tabulator-cell {{
        padding: calc(0.4rem * var(--table-scale)) calc(0.22rem * var(--table-scale));
      }}
    }}
    @media (prefers-color-scheme: dark) {{
      body {{
        background: #18120e;
        color: #f3e6da;
      }}
      .tabulator {{
        background: #18120e;
        border-color: #6f4a34;
        color: #f3e6da;
      }}
      .tabulator .tabulator-header {{
        background: #302118;
        border-bottom-color: #7a5239;
        color: #f7e6d7;
      }}
      .tabulator .tabulator-header .tabulator-col {{
        background: #302118;
      }}
      .tabulator .tabulator-header .tabulator-col:hover,
      .tabulator .tabulator-header .tabulator-col.tabulator-sortable:hover {{
        background: #3b281d !important;
      }}
      .tabulator .tabulator-header .tabulator-col .tabulator-header-filter input,
      .tabulator .tabulator-header .tabulator-col .tabulator-header-filter select {{
        background: #18120e;
        border-color: #6f4a34;
        color: #f3e6da;
      }}
      .tabulator .tabulator-tableholder .tabulator-table {{
        background: #18120e;
        color: #f3e6da;
      }}
      .tabulator .tabulator-row {{
        background: #18120e;
        border-bottom-color: #2b2019;
      }}
      .tabulator .tabulator-row:nth-child(even) {{
        background: #1f1712;
      }}
      .tabulator .tabulator-row:hover {{
        background: #2b2019;
      }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <div class="controls" id="controls">
      <div class="toggle-list">
        <label class="toggle"><input type="checkbox" data-column-toggle="company">Company</label>
        <label class="toggle"><input type="checkbox" data-column-toggle="model">Model</label>
        <label class="toggle"><input type="checkbox" data-column-toggle="agent">Agent</label>
        <label class="toggle"><input type="checkbox" data-column-toggle="web_access">Internet</label>
        <label class="toggle"><input type="checkbox" data-column-toggle="overall">Overall</label>
        <label class="toggle"><input type="checkbox" data-column-toggle="estimated_cost_usd">Cost</label>
        <label class="toggle"><input type="checkbox" data-column-toggle="elapsed_s">Time</label>
        <label class="toggle"><input type="checkbox" data-column-toggle="total_tokens">Tokens</label>
        <label class="toggle"><input type="checkbox" data-column-toggle="easy">Easy</label>
        <label class="toggle"><input type="checkbox" data-column-toggle="medium">Medium</label>
        <label class="toggle"><input type="checkbox" data-column-toggle="hard">Hard</label>
        <label class="toggle"><input type="checkbox" data-column-toggle="insane">Insane</label>
      </div>
    </div>
    <div class="table-shell">
      <div id="harness-table"></div>
    </div>
  </div>

  <script id="payload" type="application/json">{payload_json}</script>
  <script src="https://unpkg.com/tabulator-tables@6.3.1/dist/js/tabulator.min.js"></script>
  <script>
    const allHarnesses = JSON.parse(document.getElementById("payload").textContent);
    let harnessTable = null;
    const columnToggles = Array.from(document.querySelectorAll("[data-column-toggle]"));

    function percentText(value) {{
      const number = Number(value);
      if (!Number.isFinite(number)) {{
        return "";
      }}
      const percent = number * 100;
      if (percent >= 99.9) {{
        return "100%";
      }}
      return `${{percent.toFixed(1)}}%`;
    }}

    function numberText(value) {{
      const number = Number(value);
      if (!Number.isFinite(number)) {{
        return "";
      }}
      return new Intl.NumberFormat("en-US").format(Math.round(number));
    }}

    function usdText(value) {{
      if (value === null || value === undefined || value === "") {{
        return "";
      }}
      const number = Number(value);
      if (!Number.isFinite(number) || number <= 0) {{
        return "";
      }}
      if (number >= 100) {{
        return `$${{number.toFixed(0)}}`;
      }}
      if (number >= 10) {{
        return `$${{number.toFixed(1)}}`;
      }}
      if (number >= 1) {{
        return `$${{number.toFixed(2)}}`;
      }}
      return `$${{number.toFixed(3)}}`;
    }}

    function scoreFormatter(cell) {{
      const value = Number(cell.getValue() || 0);
      return percentText(value);
    }}

    function numberFormatter(cell) {{
      return numberText(cell.getValue());
    }}

    function usdFormatter(cell) {{
      return usdText(cell.getValue());
    }}

    function durationText(value) {{
      const seconds = Number(value);
      if (!Number.isFinite(seconds)) {{
        return "";
      }}
      if (seconds < 60) {{
        return `${{seconds.toFixed(1)}}s`;
      }}
      const minutes = seconds / 60;
      if (minutes < 60) {{
        return `${{minutes.toFixed(1)}}m`;
      }}
      return `${{(minutes / 60).toFixed(1)}}h`;
    }}

    function durationFormatter(cell) {{
      return durationText(cell.getValue());
    }}

    function yesNoText(value) {{
      return value ? "" : "✓";
    }}

    function yesNoFormatter(cell) {{
      return yesNoText(Boolean(cell.getValue()));
    }}

    function textHeaderFilter(headerValue, rowValue) {{
      const needle = String(headerValue || "").trim().toLowerCase();
      if (!needle) {{
        return true;
      }}
      return String(rowValue ?? "").toLowerCase().includes(needle);
    }}

    function percentHeaderFilter(headerValue, rowValue) {{
      const needle = String(headerValue || "").trim().toLowerCase();
      if (!needle) {{
        return true;
      }}
      return percentText(rowValue).toLowerCase().includes(needle);
    }}

    function numberHeaderFilter(headerValue, rowValue) {{
      const needle = String(headerValue || "").trim().toLowerCase();
      if (!needle) {{
        return true;
      }}
      return numberText(rowValue).toLowerCase().includes(needle);
    }}

    function usdHeaderFilter(headerValue, rowValue) {{
      const needle = String(headerValue || "").trim().toLowerCase();
      if (!needle) {{
        return true;
      }}
      return usdText(rowValue).toLowerCase().includes(needle);
    }}

    function durationHeaderFilter(headerValue, rowValue) {{
      const needle = String(headerValue || "").trim().toLowerCase();
      if (!needle) {{
        return true;
      }}
      return durationText(rowValue).toLowerCase().includes(needle);
    }}

    function booleanHeaderFilter(headerValue, rowValue) {{
      const needle = String(headerValue || "").trim().toLowerCase();
      if (!needle) {{
        return true;
      }}
      if (["yes", "y", "true", "1", "check", "checked", "offline", "no web", "no checks", "✓"].includes(needle)) {{
        return !Boolean(rowValue);
      }}
      if (["no", "n", "false", "0", "uncheck", "unchecked", "web", "with web", "blank", "empty"].includes(needle)) {{
        return Boolean(rowValue);
      }}
      return false;
    }}

    const baseColumnSpecs = [
      {{title: "Company", field: "company", minWidth: 88, widthGrow: 1, widthShrink: 1, headerFilter: "input", headerFilterFunc: textHeaderFilter}},
      {{title: "Model", field: "model", minWidth: 128, widthGrow: 5, widthShrink: 3, headerFilter: "input", headerFilterFunc: textHeaderFilter}},
      {{title: "Agent", field: "agent", minWidth: 60, widthGrow: 1, widthShrink: 2, headerFilter: "input", headerFilterFunc: textHeaderFilter}},
      {{title: "Internet", field: "web_access", formatter: yesNoFormatter, minWidth: 54, widthGrow: 0, widthShrink: 1, hozAlign: "center", headerHozAlign: "center", headerFilter: "input", headerFilterFunc: booleanHeaderFilter}},
      {{title: "Overall", field: "overall", sorter: "number", formatter: scoreFormatter, minWidth: 68, width: 76, widthGrow: 0, widthShrink: 1, hozAlign: "left", headerFilter: "input", headerFilterFunc: percentHeaderFilter}},
      {{title: "Cost", field: "estimated_cost_usd", sorter: "number", formatter: usdFormatter, minWidth: 68, widthGrow: 1, widthShrink: 1, hozAlign: "left", headerFilter: "input", headerFilterFunc: usdHeaderFilter}},
      {{title: "Time", field: "elapsed_s", sorter: "number", formatter: durationFormatter, minWidth: 72, widthGrow: 1, widthShrink: 1, hozAlign: "left", headerFilter: "input", headerFilterFunc: durationHeaderFilter}},
      {{title: "Tokens", field: "total_tokens", sorter: "number", formatter: numberFormatter, minWidth: 86, widthGrow: 1, widthShrink: 1, hozAlign: "left", headerFilter: "input", headerFilterFunc: numberHeaderFilter}},
      {{title: "Easy", field: "easy", sorter: "number", formatter: scoreFormatter, minWidth: 70, widthGrow: 1, widthShrink: 1, hozAlign: "left", headerFilter: "input", headerFilterFunc: percentHeaderFilter}},
      {{title: "Medium", field: "medium", sorter: "number", formatter: scoreFormatter, minWidth: 74, widthGrow: 1, widthShrink: 1, hozAlign: "left", headerFilter: "input", headerFilterFunc: percentHeaderFilter}},
      {{title: "Hard", field: "hard", sorter: "number", formatter: scoreFormatter, minWidth: 70, widthGrow: 1, widthShrink: 1, hozAlign: "left", headerFilter: "input", headerFilterFunc: percentHeaderFilter}},
      {{title: "Insane", field: "insane", sorter: "number", formatter: scoreFormatter, minWidth: 76, widthGrow: 1, widthShrink: 1, hozAlign: "left", headerFilter: "input", headerFilterFunc: percentHeaderFilter}},
    ];
    const baseDefaultFields = ["model", "agent", "web_access", "overall", "estimated_cost_usd", "elapsed_s", "easy", "medium", "hard", "insane"];
    const compactDefaultFields = ["model", "agent", "web_access", "overall", "estimated_cost_usd", "elapsed_s"];
    const mobileDefaultFields = ["model", "agent", "web_access", "overall", "estimated_cost_usd", "elapsed_s"];
    const columnPrefsKey = "cad_bench_visible_columns";
    let userCustomizedColumns = false;
    let tableScale = 1;
    let fontsReadyApplied = false;
    const widthMeasureCanvas = document.createElement("canvas");
    const widthMeasureContext = widthMeasureCanvas.getContext("2d");

    function isMobileScreen() {{
      return window.matchMedia("(max-width: 640px)").matches;
    }}

    function tableAvailableWidth() {{
      const shell = document.querySelector(".table-shell");
      return Math.max(
        0,
        Math.floor(shell?.clientWidth || window.innerWidth || 0),
      );
    }}

    function rootFontSizePx() {{
      return Number.parseFloat(getComputedStyle(document.documentElement).fontSize || "16") || 16;
    }}

    function bodyFontSizePx(scale = tableScale) {{
      return rootFontSizePx() * (isMobileScreen() ? 0.82 : 0.95) * scale;
    }}

    function headerFontSizePx(scale = tableScale) {{
      return rootFontSizePx() * (isMobileScreen() ? 0.8 : 0.95) * scale;
    }}

    function horizontalCellPaddingPx(scale = tableScale) {{
      return rootFontSizePx() * (isMobileScreen() ? 0.5 : 1.6) * scale;
    }}

    function horizontalHeaderPaddingPx(scale = tableScale) {{
      return rootFontSizePx() * (isMobileScreen() ? 0.35 : 1.6) * scale;
    }}

    function measureTextWidth(text, font) {{
      if (!widthMeasureContext) {{
        return String(text || "").length * 8;
      }}
      widthMeasureContext.font = font;
      return widthMeasureContext.measureText(String(text || "")).width;
    }}

    function bodyFont(scale = tableScale) {{
      return `400 ${{bodyFontSizePx(scale)}}px ${{getComputedStyle(document.body).fontFamily}}`;
    }}

    function headerFont(scale = tableScale) {{
      return `600 ${{headerFontSizePx(scale)}}px ${{getComputedStyle(document.body).fontFamily}}`;
    }}

    function displayTextForField(field, value) {{
      if (field === "overall" || field === "easy" || field === "medium" || field === "hard" || field === "insane") {{
        return percentText(value);
      }}
      if (field === "estimated_cost_usd") {{
        return usdText(value);
      }}
      if (field === "elapsed_s") {{
        return durationText(value);
      }}
      if (field === "total_tokens") {{
        return numberText(value);
      }}
      if (field === "web_access") {{
        return yesNoText(Boolean(value)) || " ";
      }}
      return String(value ?? "");
    }}

    function contentWidthForSpec(spec, scale = tableScale) {{
      const headerWidth = measureTextWidth(spec.title, headerFont(scale));
      const valueWidth = allHarnesses.reduce((maxWidth, row) => {{
        return Math.max(maxWidth, measureTextWidth(displayTextForField(spec.field, row?.[spec.field]), bodyFont(scale)));
      }}, 0);
      return Math.ceil(Math.max(headerWidth + horizontalHeaderPaddingPx(scale), valueWidth + horizontalCellPaddingPx(scale)));
    }}

    function resolvedWidthForSpec(spec, scale = tableScale) {{
      const minWidth = isMobileScreen() ? 0 : Number(spec.minWidth || 0) * scale;
      const fixedWidth = isMobileScreen() ? 0 : Number(spec.width || 0) * scale;
      const maxWidth = isMobileScreen() ? 0 : Number(spec.maxWidth || 0) * scale;
      let width = Math.max(minWidth, fixedWidth, contentWidthForSpec(spec, scale));
      if (maxWidth) {{
        width = Math.min(width, maxWidth);
      }}
      return Math.ceil(width);
    }}

    function minimumWidthForFields(fields, scale = tableScale) {{
      const enabled = new Set(fields);
      return baseColumnSpecs
        .filter((spec) => enabled.has(spec.field))
        .reduce((sum, spec) => sum + resolvedWidthForSpec(spec, scale), 0);
    }}

    function mobileScaleForFields(fields) {{
      const availableWidth = tableAvailableWidth();
      if (!availableWidth) {{
        return 1;
      }}
      const baseWidth = minimumWidthForFields(fields, 1);
      if (!baseWidth) {{
        return 1;
      }}
      return Math.max(0.62, Math.min(1, availableWidth / baseWidth));
    }}

    function applyTableScale(scale) {{
      tableScale = scale;
      document.documentElement.style.setProperty("--table-scale", scale.toFixed(4));
    }}

    function canFitFields(fields) {{
      const availableWidth = tableAvailableWidth();
      if (!availableWidth) {{
        return true;
      }}
      return minimumWidthForFields(fields) <= availableWidth;
    }}

    function preferredDefaultFields() {{
      if (isMobileScreen()) {{
        return mobileDefaultFields;
      }}
      return canFitFields(baseDefaultFields) ? baseDefaultFields : compactDefaultFields;
    }}

    function setToggleState(fields) {{
      const enabledFields = new Set(fields);
      for (const toggle of columnToggles) {{
        toggle.checked = enabledFields.has(toggle.dataset.columnToggle);
      }}
    }}

    function validSavedFields(fields) {{
      if (!Array.isArray(fields) || !fields.length) {{
        return [];
      }}
      const knownFields = new Set(baseColumnSpecs.map((spec) => spec.field));
      return fields.filter((field) => knownFields.has(String(field)));
    }}

    function loadSavedColumnFields() {{
      const raw = window.localStorage.getItem(columnPrefsKey);
      if (!raw) {{
        return [];
      }}
      return validSavedFields(JSON.parse(raw));
    }}

    function saveActiveColumnFields() {{
      window.localStorage.setItem(columnPrefsKey, JSON.stringify(activeColumnFields()));
    }}

    function applyInitialToggleState() {{
      const savedFields = loadSavedColumnFields();
      if (savedFields.length) {{
        userCustomizedColumns = true;
        setToggleState(savedFields);
        return;
      }}
      setToggleState(preferredDefaultFields());
    }}

    function activeColumnFields() {{
      return columnToggles
        .filter((toggle) => toggle.checked)
        .map((toggle) => toggle.dataset.columnToggle);
    }}

    function activeColumnSpecs() {{
      const fields = new Set(activeColumnFields());
      const specs = baseColumnSpecs
        .filter((spec) => fields.has(spec.field))
        .map((spec) => ({{...spec}}));
      if (!isMobileScreen()) {{
        applyTableScale(1);
        return specs.map((spec) => {{
          const desktopSpec = {{...spec}};
          delete desktopSpec.maxWidth;
          if (desktopSpec.field === "model") {{
            desktopSpec.minWidth = 280;
            desktopSpec.widthGrow = 7;
            desktopSpec.widthShrink = 1;
            delete desktopSpec.width;
          }} else if (desktopSpec.field === "agent") {{
            desktopSpec.minWidth = Math.max(90, Number(desktopSpec.minWidth || 0));
            desktopSpec.widthGrow = 1;
            desktopSpec.widthShrink = 1;
            delete desktopSpec.width;
          }} else if (desktopSpec.field === "web_access") {{
            desktopSpec.minWidth = 104;
            desktopSpec.width = 104;
            desktopSpec.widthGrow = 0;
            desktopSpec.widthShrink = 1;
          }} else {{
            desktopSpec.minWidth = Math.max(92, Number(desktopSpec.minWidth || 0));
            desktopSpec.widthGrow = 1;
            desktopSpec.widthShrink = 1;
            delete desktopSpec.width;
          }}
          return desktopSpec;
        }});
      }}
      const scale = mobileScaleForFields([...fields]);
      applyTableScale(scale);
      return specs.map((spec) => {{
        const width = resolvedWidthForSpec(spec, scale);
        return {{
          ...spec,
          width,
          minWidth: width,
          maxWidth: width,
          widthGrow: 0,
          widthShrink: 0,
        }};
      }});
    }}

    let tableReady = false;

    function buildTable() {{
      applyInitialToggleState();
      harnessTable = new Tabulator("#harness-table", {{
        data: allHarnesses,
        layout: "fitData",
        index: "harness_id",
        initialSort: [{{column: "overall", dir: "desc"}}],
        downloadRowRange: "active",
        columnDefaults: {{
          headerHozAlign: "left",
          vertAlign: "middle",
        }},
        columns: activeColumnSpecs(),
      }});
      harnessTable.on("tableBuilt", () => {{
        tableReady = true;
        syncAfterFontsReady();
        syncOptionalColumns();
      }});
    }}

    function syncOptionalColumns() {{
      if (!tableReady || !harnessTable) {{
        return;
      }}
      Promise.resolve(harnessTable.setColumns(activeColumnSpecs())).then(() => harnessTable.redraw(true));
    }}

    function syncAfterFontsReady() {{
      if (fontsReadyApplied || !document.fonts?.ready) {{
        return;
      }}
      document.fonts.ready.then(() => {{
        fontsReadyApplied = true;
        syncOptionalColumns();
      }});
    }}

    for (const toggle of columnToggles) {{
      toggle.addEventListener("change", () => {{
        userCustomizedColumns = true;
        saveActiveColumnFields();
        syncOptionalColumns();
      }});
    }}
    window.addEventListener("resize", () => {{
      if (!tableReady || !harnessTable) {{
        return;
      }}
      if (!userCustomizedColumns) {{
        const preferredFields = preferredDefaultFields();
        const activeFields = activeColumnFields();
        if (preferredFields.join("|") !== activeFields.join("|")) {{
          setToggleState(preferredFields);
          syncOptionalColumns();
          return;
        }}
      }}
      harnessTable.redraw(true);
    }});
    buildTable();
  </script>
</body>
</html>
"""


@dataclass(frozen=True)
class ImportedReport:
    report: dict[str, Any]
    run_name: str
    row_count: int
    git_head: str
    benchmark_signature: str
    tasks_signature: str
    source_path: str


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _report_benchmark_signature(
    report: dict[str, Any], *, prompt_signature: str = "", system_prompt_signature: str = ""
) -> str:
    explicit = _text(report.get("prompt_signature"))
    if explicit:
        return explicit
    payload = {
        "prompt_signature": _text(prompt_signature),
        "system_prompt_signature": _text(system_prompt_signature),
        "tasks": report.get("tasks", []),
    }
    return _sha256_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _collect_direct_reports(api: HfApi, repo_id: str) -> list[ImportedReport]:
    entries = list(
        api.list_repo_tree(
            repo_id=repo_id,
            repo_type="dataset",
            path_in_repo="evals/harnesses",
            recursive=True,
            token=resolve_hf_token(REPO_ROOT),
        )
    )
    reports: list[ImportedReport] = []
    for entry in entries:
        path_in_repo = _text(getattr(entry, "path", ""))
        if not path_in_repo.endswith("/report.json"):
            continue
        report_path = Path(
            hf_hub_download(
                repo_id=repo_id,
                repo_type="dataset",
                filename=path_in_repo,
                token=resolve_hf_token(REPO_ROOT),
            )
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            continue
        reports.append(
            ImportedReport(
                report=report,
                run_name=Path(path_in_repo).parent.name,
                row_count=len(report.get("rows", []))
                if isinstance(report.get("rows"), list)
                else 0,
                git_head=_text(report.get("git_head")),
                benchmark_signature=_report_benchmark_signature(report),
                tasks_signature=_sha256_bytes(
                    json.dumps(report.get("tasks", []), separators=(",", ":")).encode(
                        "utf-8"
                    )
                ),
                source_path=f"hf://{repo_id}/{path_in_repo}",
            )
        )
    return reports


def _load_local_report(path: Path) -> ImportedReport:
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise TypeError(f"report must be a JSON object: {path}")
    return ImportedReport(
        report=report,
        run_name=path.parent.name,
        row_count=len(report.get("rows", []))
        if isinstance(report.get("rows"), list)
        else 0,
        git_head=_text(report.get("git_head")),
        benchmark_signature=_report_benchmark_signature(report),
        tasks_signature=_sha256_bytes(
            json.dumps(report.get("tasks", []), separators=(",", ":")).encode("utf-8")
        ),
        source_path=str(path),
    )


def _collect_hf_report(
    *,
    repo_id: str,
    path_in_repo: str,
    revision: str = "main",
) -> ImportedReport:
    report_path = Path(
        hf_hub_download(
            repo_id=repo_id,
            repo_type="dataset",
            filename=path_in_repo,
            revision=revision,
            token=resolve_hf_token(REPO_ROOT),
        )
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise TypeError(f"report must be a JSON object: hf://{repo_id}/{path_in_repo}")
    return ImportedReport(
        report=report,
        run_name=Path(path_in_repo).parent.name,
        row_count=len(report.get("rows", []))
        if isinstance(report.get("rows"), list)
        else 0,
        git_head=_text(report.get("git_head")),
        benchmark_signature=_report_benchmark_signature(report),
        tasks_signature=_sha256_bytes(
            json.dumps(report.get("tasks", []), separators=(",", ":")).encode("utf-8")
        ),
        source_path=f"hf://{repo_id}@{revision}/{path_in_repo}",
    )


def _collect_approved_reports() -> list[ImportedReport]:
    payload = json.loads(APPROVED_RUNS_JSON.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise TypeError(f"approved runs manifest must be a list: {APPROVED_RUNS_JSON}")
    reports: list[ImportedReport] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        kind = _text(item.get("kind"))
        if kind == "local_report":
            path = REPO_ROOT / _text(item.get("path"))
            reports.append(_load_local_report(path))
            continue
        if kind == "hf_report":
            repo_id = _text(item.get("repo_id"))
            path_in_repo = _text(item.get("path_in_repo"))
            revision = _text(item.get("revision")) or "main"
            reports.append(
                _collect_hf_report(
                    repo_id=repo_id,
                    path_in_repo=path_in_repo,
                    revision=revision,
                )
            )
            continue
        raise ValueError(f"unsupported approved run kind: {kind!r}")
    return reports


def _report_is_publishable(item: ImportedReport) -> bool:
    harness = item.report.get("harness", {})
    if not isinstance(harness, dict):
        return False
    rows = item.report.get("rows", [])
    if not isinstance(rows, list) or not rows:
        return False
    expected_count = int(item.report.get("benchmark_task_count") or item.row_count)
    if expected_count < FULL_BENCHMARK_TASK_COUNT:
        return False
    if item.row_count < FULL_BENCHMARK_TASK_COUNT:
        return False
    if expected_count and item.row_count < expected_count:
        return False
    if any(isinstance(row, dict) and _row_is_agent_error(row) for row in rows):
        return False
    return True


def _select_cohort(
    reports: list[ImportedReport],
) -> tuple[tuple[str, str, str, int], list[ImportedReport]]:
    cohorts: dict[tuple[str, str, str, int], list[ImportedReport]] = {}
    for item in reports:
        if not _report_is_publishable(item):
            continue
        key = (
            item.tasks_signature,
            item.git_head,
            item.benchmark_signature,
            item.row_count,
        )
        cohorts.setdefault(key, []).append(item)
    if not cohorts:
        raise RuntimeError("no publishable provenance reports found")

    def score(items: list[ImportedReport]) -> tuple[int, int, int, str]:
        harnesses = {
            _text((item.report.get("harness") or {}).get("id")) for item in items
        }
        latest_stamp = max(_text(item.report.get("timestamp_utc")) for item in items)
        row_count = max(item.row_count for item in items)
        return (len(harnesses), len(items), row_count, latest_stamp)

    cohort_key, cohort_reports = max(cohorts.items(), key=lambda item: score(item[1]))
    return cohort_key, cohort_reports


def _select_publishable_reports(reports: list[ImportedReport]) -> list[ImportedReport]:
    publishable = [item for item in reports if _report_is_publishable(item)]
    if not publishable:
        raise RuntimeError("no publishable provenance reports found")
    publishable.sort(
        key=lambda item: (
            _text((item.report.get("harness") or {}).get("id")),
            _text(item.report.get("timestamp_utc")),
            item.source_path,
        )
    )
    return publishable


def _task_rows_from_report(report: dict[str, Any], run_name: str) -> list[dict[str, Any]]:
    harness = report.get("harness", {})
    rows = report.get("rows", [])
    if not isinstance(harness, dict) or not isinstance(rows, list):
        return []
    provider = _text(harness.get("provider"))
    model_name = _text(harness.get("model"))
    access = _text(harness.get("access"))
    reasoning_effort = _text(harness.get("reasoning_effort"))
    strategy = _text(harness.get("strategy"))
    harness_id = _text(harness.get("id"))
    timestamp_utc = _text(report.get("timestamp_utc"))
    task_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        metrics = _canonical_metrics(row.get("metrics", {}))
        usage_payload = row.get("usage", {})
        raw_usage = (
            usage_payload.get("raw_usage")
            if isinstance(usage_payload, dict)
            else None
        )
        if isinstance(raw_usage, dict):
            normalized_usage = usage_with_cost(
                provider,
                model_name,
                raw_usage,
                access=access,
            )
        elif isinstance(usage_payload, dict) and any(
            key in usage_payload
            for key in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "total_tokens",
                "estimated_cost_usd",
                "cost_is_precise",
                "cost_source",
            )
        ):
            normalized_usage = usage_with_cost(
                provider,
                model_name,
                usage_payload,
                access=access,
            )
        else:
            normalized_usage = usage_with_cost(
                provider,
                model_name,
                usage_payload,
                access=access,
            )
        task_rows.append(
            {
                "row_id": f"{harness_id}:{_text(row.get('task_id'))}",
                "harness_id": harness_id,
                "company": _company_name(provider),
                "provider": provider,
                "agent": _agent_name(provider, strategy),
                "model": model_name,
                "access": access,
                "web_access": _has_web_access(access),
                "reasoning_effort": reasoning_effort,
                "strategy": strategy,
                "timestamp_utc": timestamp_utc,
                "run_name": run_name,
                "task_id": _text(row.get("task_id")),
                "task_index": int(_number(row.get("index"))),
                "difficulty": _text(row.get("difficulty")),
                "elapsed_s": round(_number(row.get("elapsed_s")), 3),
                "reward": round(_number(metrics.get("reward")), 6),
                "overall_score": round(_number(metrics.get("overall_score")), 6),
                "task_score": round(_number(metrics.get("task_score")), 6),
                "build_success": round(_number(metrics.get("build_success")), 6),
                "submission_exists": round(_number(metrics.get("submission_exists")), 6),
                "score_error": _text(
                    row.get("score_error") or metrics.get("score_error")
                ),
                "error": _text(row.get("error")),
                "returncode": int(_number(row.get("returncode"))),
                "session_id": _text(row.get("session_id")),
                "status": _status_for_row(row),
                "input_tokens": int(_number(normalized_usage.get("input_tokens"))),
                "cached_input_tokens": int(
                    _number(normalized_usage.get("cached_input_tokens"))
                ),
                "output_tokens": int(_number(normalized_usage.get("output_tokens"))),
                "total_tokens": int(_number(normalized_usage.get("total_tokens"))),
                "estimated_cost_usd": _listed_cost(
                    normalized_usage.get("estimated_cost_usd")
                ),
                "cost_is_precise": bool(normalized_usage.get("cost_is_precise")),
                "cost_source": _text(normalized_usage.get("cost_source")),
            }
        )
    return task_rows


def main() -> int:
    global APPROVED_RUNS_JSON, WEBSITE_HTML
    parser = argparse.ArgumentParser(
        description="Render the published results site from an approved provenance manifest."
    )
    parser.add_argument(
        "--approved-runs-json",
        type=Path,
        required=True,
        help="Path to the site manifest containing approved provenance report references.",
    )
    parser.add_argument(
        "--output-html",
        type=Path,
        required=True,
        help="Path to write the rendered static site HTML.",
    )
    parser.add_argument(
        "--keep-extracted",
        action="store_true",
        help="Deprecated no-op kept for CLI compatibility; imports now read direct reports only.",
    )
    args = parser.parse_args()

    if args.keep_extracted:
        print("--keep-extracted is ignored: imports now read direct reports only")

    APPROVED_RUNS_JSON = args.approved_runs_json.resolve()
    WEBSITE_HTML = args.output_html.resolve()
    WEBSITE_HTML.parent.mkdir(parents=True, exist_ok=True)

    task_rows: list[dict[str, Any]] = []
    reports = _collect_approved_reports()
    if reports:
        for item in _select_publishable_reports(reports):
            task_rows.extend(_task_rows_from_report(item.report, item.run_name))

    source_label = f"approved://{APPROVED_RUNS_JSON.name}"
    payload = _build_payload_from_task_rows(task_rows, source_label)
    payload["approved_runs_manifest"] = APPROVED_RUNS_JSON.name

    WEBSITE_HTML.write_text(_html_template(payload), encoding="utf-8")
    print(
        f"Imported {payload['total_harnesses']} harnesses / {payload['total_task_runs']} task rows from approved runs"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
