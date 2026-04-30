import importlib.util
from pathlib import Path
import sys

import pytest

from bench import runner as bench
from harnesses.costs import normalize_usage, pricing_for, usage_with_cost


def _row(difficulty: str, overall_score: float) -> dict[str, object]:
    reward = 0.05 + 0.95 * overall_score
    return {
        "difficulty": difficulty,
        "metrics": {
            "reward": reward,
            "overall_score": overall_score,
            "task_score": overall_score,
            "build_success": 1.0,
            "submission_exists": 1.0,
        },
        "usage": {},
    }


def _load_site_builder():
    path = Path(__file__).resolve().parent.parent / "scripts" / "render_site.py"
    spec = importlib.util.spec_from_file_location("render_site", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_summarize_harness_uses_overall_score_without_scaling() -> None:
    rows = [
        _row("easy", 1.000),
        _row("medium", 1.000),
        _row("hard", 0.500),
        _row("insane", 0.250),
    ]

    summary = bench.summarize_harness(rows)

    assert summary["by_difficulty"]["easy"] == pytest.approx(1.0)
    assert summary["by_difficulty"]["medium"] == pytest.approx(1.0)
    assert summary["by_difficulty"]["hard"] == pytest.approx(0.500)
    assert summary["by_difficulty"]["insane"] == pytest.approx(0.250)
    assert summary["benchmark_score"] == pytest.approx((1.0 + 2.0 + 1.5 + 1.0) / 10.0)
    assert "avg_reward" not in summary
    assert "build_rate" not in summary


def test_results_site_summary_matches_harness_summary_without_scaling() -> None:
    site_builder = _load_site_builder()
    rows = [
        _row("easy", 1.000),
        _row("medium", 1.000),
        _row("hard", 0.500),
        _row("insane", 0.250),
    ]

    summary = site_builder._summarize_rows(rows)

    assert summary["by_difficulty"]["easy"] == pytest.approx(1.0)
    assert summary["by_difficulty"]["medium"] == pytest.approx(1.0)
    assert summary["by_difficulty"]["hard"] == pytest.approx(0.500)
    assert summary["by_difficulty"]["insane"] == pytest.approx(0.250)
    assert summary["benchmark_score"] == pytest.approx((1.0 + 2.0 + 1.5 + 1.0) / 10.0)
    assert "avg_reward" not in summary
    assert "build_rate" not in summary


def test_results_site_filters_zero_overall_harness_rows() -> None:
    site_builder = _load_site_builder()
    payload = site_builder._build_payload_from_task_rows(
        [
            {
                "harness_id": "openai/gpt-5.4-offline-high",
                "provider": "openai",
                "model": "gpt-5.4",
                "access": "offline",
                "reasoning_effort": "high",
                "strategy": "one_shot_code",
                "timestamp_utc": "2026-04-11T00:00:00+00:00",
                "run_name": "openai_good",
                "task_id": "t1",
                "task_index": 1,
                "difficulty": "easy",
                "reward": 0.0,
                "overall_score": 0.0,
                "task_score": 0.0,
                "build_success": 0.0,
                "submission_exists": 0.0,
                "score_error": "invalid syntax",
                "error": "",
                "usage": {},
            },
            {
                "harness_id": "openai/gpt-5.4-offline-high",
                "provider": "openai",
                "model": "gpt-5.4",
                "access": "offline",
                "reasoning_effort": "high",
                "strategy": "one_shot_code",
                "timestamp_utc": "2026-04-11T00:00:00+00:00",
                "run_name": "openai_good",
                "task_id": "t2",
                "task_index": 2,
                "difficulty": "medium",
                "reward": 1.0,
                "overall_score": 1.0,
                "task_score": 1.0,
                "build_success": 1.0,
                "submission_exists": 1.0,
                "score_error": "",
                "error": "",
                "usage": {},
            },
            {
                "harness_id": "openai/gpt-5.4-mini-offline-high",
                "provider": "openai",
                "model": "gpt-5.4-mini",
                "access": "offline",
                "reasoning_effort": "high",
                "strategy": "one_shot_code",
                "timestamp_utc": "2026-04-11T00:00:00+00:00",
                "run_name": "openai_bad",
                "task_id": "t1",
                "task_index": 1,
                "difficulty": "easy",
                "reward": 0.0,
                "overall_score": 0.0,
                "task_score": 0.0,
                "build_success": 0.0,
                "submission_exists": 0.0,
                "score_error": "",
                "error": "openai exit 1: upstream failed",
                "usage": {},
            },
            {
                "harness_id": "openai/gpt-5.4-mini-offline-high",
                "provider": "openai",
                "model": "gpt-5.4-mini",
                "access": "offline",
                "reasoning_effort": "high",
                "strategy": "one_shot_code",
                "timestamp_utc": "2026-04-11T00:00:00+00:00",
                "run_name": "openai_bad",
                "task_id": "t2",
                "task_index": 2,
                "difficulty": "medium",
                "reward": 0.0,
                "overall_score": 0.0,
                "task_score": 0.0,
                "build_success": 0.0,
                "submission_exists": 0.0,
                "score_error": "",
                "error": "openai exit 1: upstream failed",
                "usage": {},
            },
        ],
        "test-source",
    )

    assert [row["harness_id"] for row in payload["harnesses"]] == [
        "openai/gpt-5.4-offline-high",
    ]
    assert len(payload["task_runs"]) == 4
    assert payload["task_runs"][0]["score_error"] == "invalid syntax"
    assert payload["task_runs"][0]["error"] == ""
    assert all(row["overall"] > 0.0 for row in payload["harnesses"])


def test_results_site_scores_missing_benchmark_tasks_as_zero() -> None:
    site_builder = _load_site_builder()
    payload = site_builder._build_payload_from_task_rows(
        [
            {
                "harness_id": "openai/gpt-5.4-offline-high",
                "provider": "openai",
                "model": "gpt-5.4",
                "access": "offline",
                "reasoning_effort": "high",
                "strategy": "one_shot_code",
                "timestamp_utc": "2026-04-11T00:00:00+00:00",
                "run_name": "partial",
                "task_id": "cube_20mm_z_minus",
                "task_index": 1,
                "difficulty": "easy",
                "reward": 1.0,
                "overall_score": 1.0,
                "task_score": 1.0,
                "build_success": 1.0,
                "submission_exists": 1.0,
                "score_error": "",
                "error": "",
                "usage": {},
            },
        ],
        "test-source",
    )

    row = payload["harnesses"][0]
    assert row["task_count"] == 1
    assert row["expected_task_count"] == 17
    assert row["missing_task_count"] == 16
    assert row["easy"] == pytest.approx(1.0 / 3.0)
    assert row["medium"] == 0.0
    assert row["hard"] == 0.0
    assert row["insane"] == 0.0
    assert row["overall"] == pytest.approx((1.0 / 3.0) / 10.0)


def test_results_site_displays_openai_codex_backend_as_openai_no_agent() -> None:
    site_builder = _load_site_builder()
    payload = site_builder._build_payload_from_task_rows(
        [
            {
                "harness_id": "openai/gpt-5.5-offline-low",
                "provider": "openai_codex",
                "model": "gpt-5.5",
                "access": "offline",
                "reasoning_effort": "low",
                "strategy": "one_shot_code",
                "timestamp_utc": "2026-04-26T00:00:00+00:00",
                "run_name": "codex_backend",
                "task_id": "t1",
                "task_index": 1,
                "difficulty": "easy",
                "reward": 1.0,
                "overall_score": 1.0,
                "task_score": 1.0,
                "build_success": 1.0,
                "submission_exists": 1.0,
                "score_error": "",
                "error": "",
                "usage": {},
            }
        ],
        "test-source",
    )

    row = payload["harnesses"][0]
    assert row["harness_id"] == "openai/gpt-5.5-offline-low"
    assert row["company"] == "OpenAI"
    assert row["agent"] == "none"


def test_results_site_displays_api_providers_as_no_agent() -> None:
    site_builder = _load_site_builder()
    payload = site_builder._build_payload_from_task_rows(
        [
            {
                "harness_id": "gemini/gemini-3.1-pro-preview-offline-none",
                "provider": "gemini",
                "model": "gemini-3.1-pro-preview",
                "access": "offline",
                "reasoning_effort": "",
                "strategy": "one_shot_code",
                "timestamp_utc": "2026-04-28T00:00:00+00:00",
                "run_name": "gemini_api",
                "task_id": "t1",
                "task_index": 1,
                "difficulty": "easy",
                "reward": 1.0,
                "overall_score": 1.0,
                "task_score": 1.0,
                "build_success": 1.0,
                "submission_exists": 1.0,
                "score_error": "",
                "error": "",
                "usage": {},
            },
            {
                "harness_id": "deepseek/deepseek-v4-pro-offline-none",
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
                "access": "offline",
                "reasoning_effort": "",
                "strategy": "one_shot_code",
                "timestamp_utc": "2026-04-28T00:00:00+00:00",
                "run_name": "deepseek_api",
                "task_id": "t1",
                "task_index": 1,
                "difficulty": "easy",
                "reward": 1.0,
                "overall_score": 1.0,
                "task_score": 1.0,
                "build_success": 1.0,
                "submission_exists": 1.0,
                "score_error": "",
                "error": "",
                "usage": {},
            },
        ],
        "test-source",
    )

    agents = {row["harness_id"]: row["agent"] for row in payload["harnesses"]}
    assert agents["gemini/gemini-3.1-pro-preview-offline-none"] == "none"
    assert agents["deepseek/deepseek-v4-pro-offline-none"] == "none"


def test_results_site_sums_elapsed_time_for_latest_tasks() -> None:
    site_builder = _load_site_builder()
    payload = site_builder._build_payload_from_task_rows(
        [
            {
                "harness_id": "openai/gpt-5.4-offline-low",
                "provider": "openai",
                "model": "gpt-5.4",
                "access": "offline",
                "reasoning_effort": "low",
                "strategy": "one_shot_code",
                "timestamp_utc": "2026-04-26T00:00:00+00:00",
                "run_name": "old",
                "task_id": "t1",
                "task_index": 1,
                "difficulty": "easy",
                "elapsed_s": 100.0,
                "reward": 0.0,
                "overall_score": 0.0,
                "task_score": 0.0,
                "build_success": 0.0,
                "submission_exists": 0.0,
                "score_error": "",
                "error": "",
                "usage": {},
            },
            {
                "harness_id": "openai/gpt-5.4-offline-low",
                "provider": "openai",
                "model": "gpt-5.4",
                "access": "offline",
                "reasoning_effort": "low",
                "strategy": "one_shot_code",
                "timestamp_utc": "2026-04-26T00:00:01+00:00",
                "run_name": "new",
                "task_id": "t1",
                "task_index": 1,
                "difficulty": "easy",
                "elapsed_s": 1.25,
                "reward": 1.0,
                "overall_score": 1.0,
                "task_score": 1.0,
                "build_success": 1.0,
                "submission_exists": 1.0,
                "score_error": "",
                "error": "",
                "usage": {},
            },
            {
                "harness_id": "openai/gpt-5.4-offline-low",
                "provider": "openai",
                "model": "gpt-5.4",
                "access": "offline",
                "reasoning_effort": "low",
                "strategy": "one_shot_code",
                "timestamp_utc": "2026-04-26T00:00:00+00:00",
                "run_name": "old",
                "task_id": "t2",
                "task_index": 2,
                "difficulty": "medium",
                "elapsed_s": 2.5,
                "reward": 1.0,
                "overall_score": 1.0,
                "task_score": 1.0,
                "build_success": 1.0,
                "submission_exists": 1.0,
                "score_error": "",
                "error": "",
                "usage": {},
            },
        ],
        "test-source",
    )

    assert payload["harnesses"][0]["elapsed_s"] == pytest.approx(3.75)


def test_usage_normalization_tracks_cached_input_separately() -> None:
    usage = normalize_usage(
        {
            "input_tokens": 1000,
            "input_tokens_details": {"cached_tokens": 250},
            "output_tokens": 100,
        }
    )

    assert usage.input_tokens == 1000
    assert usage.cached_input_tokens == 250
    assert usage.output_tokens == 100


def test_runtime_usage_normalization_adds_cache_read_to_total_input() -> None:
    usage = normalize_usage(
        {
            "input": 199,
            "output": 6,
            "cacheRead": 1664,
            "cacheWrite": 0,
            "totalTokens": 1869,
        }
    )

    assert usage.input_tokens == 1863
    assert usage.cached_input_tokens == 1664
    assert usage.output_tokens == 6
    assert usage.total_tokens == 1869


def test_codex_subscription_credits_are_converted_to_usd() -> None:
    usage = usage_with_cost(
        "codex",
        "gpt-5.4",
        {
            "input_tokens": 1_000_000,
            "cached_input_tokens": 100_000,
            "output_tokens": 10_000,
        },
    )

    assert usage["estimated_cost_usd"] == pytest.approx(900_000 * 2.50 / 1_000_000 + 100_000 * 0.25 / 1_000_000 + 10_000 * 15.00 / 1_000_000)
    assert usage["cost_is_precise"] is True


def test_openai_codex_gpt_5_5_pricing_matches_rate_card() -> None:
    pricing = pricing_for("openai_codex", "gpt-5.5")

    assert pricing is not None
    assert pricing.input_usd_per_million == pytest.approx(5.00)
    assert pricing.cached_input_usd_per_million == pytest.approx(0.50)
    assert pricing.output_usd_per_million == pytest.approx(30.00)
    assert pricing.source == "openai_api_standard_pricing"


def test_cost_is_estimated_without_explicit_cached_breakdown() -> None:
    usage = usage_with_cost("openai", "gpt-5.4", {"input_tokens": 1000, "output_tokens": 100})

    assert usage["estimated_cost_usd"] == pytest.approx(
        (1000 * 2.50 + 100 * 15.00) / 1_000_000
    )
    assert usage["cost_is_precise"] is False


def test_openai_tool_access_cost_is_token_only_not_precise() -> None:
    usage = usage_with_cost(
        "openai",
        "gpt-5.4",
        {
            "input_tokens": 1000,
            "cached_input_tokens": 100,
            "output_tokens": 100,
        },
        access="web_ci",
    )

    assert usage["estimated_cost_usd"] == pytest.approx(
        (900 * 2.50 + 100 * 0.25 + 100 * 15.00) / 1_000_000
    )
    assert usage["cost_is_precise"] is False
    assert usage["cost_source"].endswith("_token_only_excludes_uncounted_tool_charges")


def test_openai_counted_tool_charges_are_included_in_estimate() -> None:
    usage = usage_with_cost(
        "openai",
        "gpt-5.4",
        {
            "input_tokens": 1000,
            "cached_input_tokens": 100,
            "output_tokens": 100,
            "web_search_calls": 2,
            "code_interpreter_sessions": 1,
        },
        access="web_ci",
    )

    token_cost = (900 * 2.50 + 100 * 0.25 + 100 * 15.00) / 1_000_000
    assert usage["estimated_cost_usd"] == pytest.approx(token_cost + 0.02 + 0.03)
    assert usage["estimated_tool_cost_usd"] == pytest.approx(0.05)
    assert usage["cost_is_precise"] is False
    assert usage["cost_source"].endswith("_includes_counted_tool_charges")


def test_unknown_preview_model_cost_is_not_marked_precise() -> None:
    usage = usage_with_cost("codex", "gpt-5.3-codex-spark", {"input_tokens": 1000, "output_tokens": 1000})

    assert usage["estimated_cost_usd"] is None
    assert usage["cost_is_precise"] is False
    assert usage["cost_source"] == "unknown_model_pricing"


def test_import_preserves_logged_imprecise_costs() -> None:
    path = Path(__file__).resolve().parent.parent / "scripts" / "render_site.py"
    spec = importlib.util.spec_from_file_location("render_site", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["render_site"] = module
    spec.loader.exec_module(module)
    row_usage = usage_with_cost(
        "openai",
        "gpt-5.4",
        {"input_tokens": 1000, "output_tokens": 100},
    )

    rows = module._task_rows_from_report(
        {
            "harness": {
                "id": "openai/gpt-5.4-offline-high",
                "provider": "openai",
                "model": "gpt-5.4",
                "access": "offline",
                "reasoning_effort": "high",
                "strategy": "one_shot_code",
            },
            "timestamp_utc": "2026-04-12T00:00:00+00:00",
            "rows": [
                {
                    "task_id": "cube_20mm_z_minus",
                    "index": 1,
                    "difficulty": "easy",
                    "elapsed_s": 1.0,
                    "metrics": {
                        "reward": 1.0,
                        "overall_score": 1.0,
                        "task_score": 1.0,
                        "build_success": 1.0,
                        "submission_exists": 1.0,
                    },
                    "error": "",
                    "usage": row_usage,
                }
            ],
        },
        "run_name",
    )

    assert rows[0]["estimated_cost_usd"] == pytest.approx(
        (1000 * 2.50 + 100 * 15.00) / 1_000_000
    )


def test_import_recomputes_usage_from_raw_usage_for_legacy_reports() -> None:
    path = Path(__file__).resolve().parent.parent / "scripts" / "render_site.py"
    spec = importlib.util.spec_from_file_location("render_site", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["render_site_legacy_cost"] = module
    spec.loader.exec_module(module)

    rows = module._task_rows_from_report(
        {
            "harness": {
                "id": "pi/gpt-5.4-offline-low",
                "provider": "pi",
                "model": "gpt-5.4",
                "access": "offline",
                "reasoning_effort": "low",
                "strategy": "agent_step",
            },
            "timestamp_utc": "2026-04-19T00:00:00+00:00",
            "rows": [
                {
                    "task_id": "cube_20mm_z_minus",
                    "index": 1,
                    "difficulty": "easy",
                    "elapsed_s": 1.0,
                    "metrics": {
                        "reward": 1.0,
                        "overall_score": 1.0,
                        "task_score": 1.0,
                        "build_success": 1.0,
                        "submission_exists": 1.0,
                    },
                    "usage": {
                        "input_tokens": 199,
                        "cached_input_tokens": 199,
                        "output_tokens": 6,
                        "total_tokens": 1869,
                        "estimated_cost_usd": 0.00013975,
                        "cost_is_precise": True,
                        "raw_usage": {
                            "input": 199,
                            "output": 6,
                            "cacheRead": 1664,
                            "cacheWrite": 0,
                            "totalTokens": 1869,
                        },
                    },
                }
            ],
        },
        "legacy_run",
    )

    assert rows[0]["input_tokens"] == 1863
    assert rows[0]["cached_input_tokens"] == 1664
    assert rows[0]["estimated_cost_usd"] == pytest.approx(
        (199 * 2.50 + 1664 * 0.25 + 6 * 15.00) / 1_000_000
    )
    assert rows[0]["cost_is_precise"] is True


def test_import_recomputes_cost_from_legacy_normalized_usage_without_raw_usage() -> None:
    path = Path(__file__).resolve().parent.parent / "scripts" / "render_site.py"
    spec = importlib.util.spec_from_file_location("render_site", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["render_site_legacy_normalized_cost"] = module
    spec.loader.exec_module(module)

    rows = module._task_rows_from_report(
        {
            "harness": {
                "id": "openai/gpt-5.4-pro-offline-high",
                "provider": "openai",
                "model": "gpt-5.4-pro",
                "access": "offline",
                "reasoning_effort": "high",
                "strategy": "one_shot_code",
            },
            "timestamp_utc": "2026-04-23T00:00:00+00:00",
            "rows": [
                {
                    "task_id": "cube_20mm_z_minus",
                    "index": 1,
                    "difficulty": "easy",
                    "elapsed_s": 1.0,
                    "metrics": {
                        "reward": 1.0,
                        "overall_score": 1.0,
                        "task_score": 1.0,
                        "build_success": 1.0,
                        "submission_exists": 1.0,
                    },
                    "usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 0,
                        "output_tokens": 100,
                        "total_tokens": 1100,
                        "estimated_cost_usd": None,
                        "cost_is_precise": False,
                    },
                }
            ],
        },
        "legacy_normalized_run",
    )

    assert rows[0]["estimated_cost_usd"] == pytest.approx(
        (1000 * 30.00 + 100 * 180.00) / 1_000_000
    )


def test_publishable_report_allows_complete_missing_submissions() -> None:
    site_builder = _load_site_builder()
    site_builder.FULL_BENCHMARK_TASK_COUNT = 1
    report = {
        "harness": {"id": "codex/gpt-5.4-web-low", "provider": "codex"},
        "timestamp_utc": "2026-04-18T00:00:00+00:00",
        "rows": [
            {
                "task_id": "cube_20mm_z_minus",
                "error": "",
                "metrics": {
                    "submission_exists": 0.0,
                    "build_success": 0.0,
                    "reward": 0.0,
                    "overall_score": 0.0,
                    "task_score": 0.0,
                    "score_error": "missing_submission",
                },
            }
        ],
    }
    item = site_builder.ImportedReport(
        report=report,
        run_name="broken_run",
        row_count=1,
        git_head="deadbeef",
        benchmark_signature="bench",
        tasks_signature="tasks",
        source_path="hf://example/report.json",
    )
    assert site_builder._report_is_publishable(item) is True


def test_publishable_report_rejects_any_agent_error_rows() -> None:
    site_builder = _load_site_builder()
    report = {
        "harness": {"id": "openai/gpt-5.4-mini-offline-high", "provider": "openai"},
        "timestamp_utc": "2026-04-18T00:00:00+00:00",
        "benchmark_task_count": 2,
        "rows": [
            {
                "task_id": "t1",
                "error": "openai exit 1: insufficient_quota",
                "metrics": {
                    "submission_exists": 0.0,
                    "build_success": 0.0,
                    "reward": 0.0,
                    "overall_score": 0.0,
                    "task_score": 0.0,
                    "score_error": "missing_submission",
                },
            },
            {
                "task_id": "t2",
                "error": "",
                "metrics": {
                    "submission_exists": 1.0,
                    "build_success": 1.0,
                    "reward": 1.0,
                    "overall_score": 1.0,
                    "task_score": 1.0,
                    "score_error": "",
                },
            },
        ],
    }
    item = site_builder.ImportedReport(
        report=report,
        run_name="quota_failed_run",
        row_count=2,
        git_head="deadbeef",
        benchmark_signature="bench",
        tasks_signature="tasks",
        source_path="hf://example/report.json",
    )
    assert site_builder._report_is_publishable(item) is False
