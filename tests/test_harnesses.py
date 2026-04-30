from harnesses import utils as harness_utils
from conftest import builtin_harness, load_builtin_harness
from pathlib import Path
import pytest

from harnesses.costs import normalize_usage, usage_with_cost
from harnesses.openai import harnesses as openai_harnesses


def test_resolve_harness_spec_path_requires_real_path() -> None:
    resolved = harness_utils.resolve_harness_spec_path(builtin_harness("codex", "gpt-5.4", "web", "low"))
    assert resolved.name == "harnesses.py"


def test_load_harness_spec_validates_builtin_fields() -> None:
    spec = load_builtin_harness("pi", "gpt-5.4", "offline", "med")
    assert spec.harness_id == "pi/gpt-5.4-offline-med"
    assert spec.provider == "pi"
    assert spec.strategy == "agent_step"
    assert spec.model == "gpt-5.4"
    assert spec.reasoning_effort == "medium"
    assert spec.access == "offline"
    assert spec.symbol_name == "gpt_5_4_offline_med"


def test_load_harness_spec_allows_custom_provider_strategy_and_access(
    tmp_path: Path,
) -> None:
    harness_path = tmp_path / "custom_harness.py"
    harness_path.write_text(
        "\n".join(
            [
                "from harnesses.utils import HarnessSpec",
                "",
                "custom = HarnessSpec(",
                '    harness_id="custom/acme-agent",',
                '    provider="acme",',
                '    strategy="bring_your_own_runner",',
                '    model="cad-model-v1",',
                '    reasoning_effort="medium",',
                '    access="intranet",',
                ")",
                "",
            ]
        ),
        encoding="utf-8",
    )

    spec = harness_utils.load_harness_spec(f"{harness_path}:custom")

    assert spec.harness_id == "custom/acme-agent"
    assert spec.provider == "acme"
    assert spec.strategy == "bring_your_own_runner"
    assert spec.access == "intranet"


def test_openai_web_ci_builtin_and_count() -> None:
    spec = load_builtin_harness("openai", "gpt-5.4", "web_ci", "xhigh")
    assert spec.harness_id == "openai/gpt-5.4-web_ci-xhigh"
    assert spec.strategy == "one_shot_code"
    assert spec.reasoning_effort == "xhigh"
    assert spec.access == "web_ci"

    module = harness_utils.harness_module(spec)
    exported = [
        name
        for name in dir(module)
        if name.startswith("gpt_") and callable(getattr(module, name))
    ]
    assert len(exported) == len(openai_harnesses.SUPPORTED_CONFIGS)


def test_openai_gpt_5_5_api_models_are_registered() -> None:
    base = load_builtin_harness("openai", "gpt-5.5", "web_ci", "xhigh")
    pro = load_builtin_harness("openai", "gpt-5.5-pro", "offline", "high")
    mini = load_builtin_harness("openai", "gpt-5.5-mini", "web", "low")
    nano = load_builtin_harness("openai", "gpt-5.5-nano", "offline", "med")

    assert base.harness_id == "openai/gpt-5.5-web_ci-xhigh"
    assert pro.harness_id == "openai/gpt-5.5-pro-offline-high"
    assert mini.harness_id == "openai/gpt-5.5-mini-web-low"
    assert nano.harness_id == "openai/gpt-5.5-nano-offline-med"


def test_openai_codex_gpt_5_5_api_models_are_registered() -> None:
    base = load_builtin_harness("openai_codex", "gpt-5.5", "offline", "xhigh")
    spark = load_builtin_harness(
        "openai_codex", "gpt-5.3-codex-spark", "offline", "high"
    )

    assert base.harness_id == "openai/gpt-5.5-offline-xhigh"
    assert base.provider == "openai_codex"
    assert base.reasoning_effort == "xhigh"
    assert spark.harness_id == "openai/gpt-5.3-codex-spark-offline-high"
    assert spark.provider == "openai_codex"
    assert spark.reasoning_effort == "high"


def test_gemini_gemma_uses_user_prompt_only() -> None:
    spec = load_builtin_harness("gemini", "gemma-3-27b-it", "offline_nodocs", "none")

    assert spec.system_prompt is None


@pytest.mark.parametrize(
    ("provider", "model", "access", "symbol_name"),
    [
        ("kimi", "kimi-k2.6", "offline", "kimi_k2_6_offline_none"),
        ("deepseek", "deepseek-v4-pro", "offline", "deepseek_v4_pro_offline_none"),
        ("deepseek", "deepseek-v4-flash", "offline", "deepseek_v4_flash_offline_none"),
        ("gemini", "gemini-3-pro-preview", "offline", "gemini_3_pro_preview_offline_none"),
        (
            "gemini",
            "gemini-3-flash-preview",
            "offline",
            "gemini_3_flash_preview_offline_none",
        ),
        (
            "gemini",
            "gemini-3.1-pro-preview",
            "offline",
            "gemini_3_1_pro_preview_offline_none",
        ),
        (
            "gemini",
            "gemini-3.1-flash-lite-preview",
            "offline",
            "gemini_3_1_flash_lite_preview_offline_none",
        ),
        ("gemini", "gemma-3-27b-it", "offline", "gemma_3_27b_it_offline_none"),
        (
            "gemini",
            "gemma-3-27b-it",
            "offline_nodocs",
            "gemma_3_27b_it_offline_nodocs_none",
        ),
        ("gemini", "gemma-4-31b-it", "offline", "gemma_4_31b_it_offline_none"),
        (
            "openrouter",
            "inclusionai/ling-2.6-1t:free",
            "offline",
            "inclusionai_ling_2_6_1t_free_offline_none",
        ),
        (
            "openrouter",
            "google/gemma-4-26b-a4b-it:free",
            "offline",
            "google_gemma_4_26b_a4b_it_free_offline_none",
        ),
        (
            "openrouter",
            "google/gemma-4-31b-it:free",
            "offline",
            "google_gemma_4_31b_it_free_offline_none",
        ),
        (
            "openrouter",
            "tencent/hy3-preview:free",
            "offline",
            "tencent_hy3_preview_free_offline_none",
        ),
        ("vercel", "alibaba/qwen3.6-27b", "offline", "alibaba_qwen3_6_27b_offline_none"),
        (
            "vercel",
            "alibaba/qwen-3.6-max-preview",
            "offline",
            "alibaba_qwen_3_6_max_preview_offline_none",
        ),
        ("claude", "claude-opus-4-7", "offline", "claude_opus_4_7_offline_none"),
        ("claude", "claude-sonnet-4-6", "offline", "claude_sonnet_4_6_offline_none"),
    ],
)
def test_raw_api_model_harnesses_are_registered(
    provider: str, model: str, access: str, symbol_name: str
) -> None:
    spec = load_builtin_harness(provider, model, access, "none")
    assert spec.harness_id == f"{provider}/{model}-{access}-none"
    assert spec.provider == provider
    assert spec.strategy == "one_shot_code"
    assert spec.model == model
    assert spec.reasoning_effort == ""
    assert spec.access == access
    assert spec.symbol_name == symbol_name


def test_api_provider_usage_shapes_normalize_to_token_usage() -> None:
    openai_compatible = normalize_usage(
        {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}
    )
    gemini = normalize_usage(
        {
            "promptTokenCount": 11,
            "candidatesTokenCount": 13,
            "thoughtsTokenCount": 3,
            "totalTokenCount": 27,
        }
    )
    anthropic = normalize_usage({"input_tokens": 17, "output_tokens": 19})

    assert openai_compatible.input_tokens == 5
    assert openai_compatible.output_tokens == 7
    assert openai_compatible.total_tokens == 12
    assert gemini.input_tokens == 11
    assert gemini.output_tokens == 16
    assert gemini.total_tokens == 27
    assert anthropic.input_tokens == 17
    assert anthropic.output_tokens == 19


def test_known_api_provider_costs_are_estimated() -> None:
    deepseek_usage = usage_with_cost(
        "deepseek",
        "deepseek-v4-pro",
        {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
        access="offline",
    )
    claude_usage = usage_with_cost(
        "claude",
        "claude-sonnet-4-6",
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        access="offline",
    )

    assert deepseek_usage["estimated_cost_usd"] == pytest.approx(5.22)
    assert claude_usage["estimated_cost_usd"] == pytest.approx(18.0)


def test_gemini_costs_use_paid_tier_pricing_and_bill_thinking_tokens() -> None:
    usage = usage_with_cost(
        "gemini",
        "gemini-3.1-pro-preview",
        {
            "promptTokenCount": 1_000_000,
            "candidatesTokenCount": 1_000,
            "thoughtsTokenCount": 9_000,
            "totalTokenCount": 1_010_000,
        },
        access="offline",
    )

    assert usage["output_tokens"] == 10_000
    assert usage["estimated_cost_usd"] == pytest.approx(1_000_000 * 4.00 / 1_000_000 + 10_000 * 18.00 / 1_000_000)


def test_codex_builtin_count_expands_with_gpt_5_4_mini() -> None:
    spec = load_builtin_harness("codex", "gpt-5.5-mini", "offline", "xhigh")
    assert spec.harness_id == "codex/gpt-5.5-mini-offline-xhigh"
    assert spec.symbol_name == "gpt_5_5_mini_offline_xhigh"

    module = harness_utils.harness_module(spec)
    exported = [
        name
        for name in dir(module)
        if name.startswith("gpt_") and callable(getattr(module, name))
    ]
    assert len(exported) == 48


def test_pi_builtin_count_doubles_with_gpt_5_4_mini() -> None:
    spec = load_builtin_harness("pi", "gpt-5.5-nano", "web", "low")
    assert spec.harness_id == "pi/gpt-5.5-nano-web-low"
    assert spec.symbol_name == "gpt_5_5_nano_web_low"

    module = harness_utils.harness_module(spec)
    exported = [
        name
        for name in dir(module)
        if name.startswith("gpt_") and callable(getattr(module, name))
    ]
    assert len(exported) == 40


@pytest.mark.parametrize(
    "ref",
    [
        builtin_harness("openai", "gpt-5.4-pro", "web_ci", "med"),
        builtin_harness("openai", "gpt-5.4-pro", "offline", "low"),
        builtin_harness("openai", "gpt-5.5-pro", "web_ci", "med"),
        builtin_harness("openai", "gpt-5.5-pro", "offline", "low"),
    ],
)
def test_openai_invalid_pro_harnesses_are_not_registered(ref: str) -> None:
    with pytest.raises(AttributeError):
        harness_utils.load_harness_spec(ref)
