from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


CREDITS_PER_USD = 25.0
TOKENS_PER_MILLION = 1_000_000.0
WEB_SEARCH_USD_PER_1K_CALLS = 10.0
CODE_INTERPRETER_USD_PER_SESSION = 0.03


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class TokenPricing:
    input_usd_per_million: float
    cached_input_usd_per_million: float | None
    output_usd_per_million: float
    source: str
    precise: bool = True


CODEX_SUBSCRIPTION_USD_PRICING: dict[str, TokenPricing] = {
    "gpt-5.5": TokenPricing(
        input_usd_per_million=125.0 / CREDITS_PER_USD,
        cached_input_usd_per_million=12.50 / CREDITS_PER_USD,
        output_usd_per_million=750.0 / CREDITS_PER_USD,
        source="openai_codex_token_rate_card_credits_converted_to_usd",
    ),
    "gpt-5.4": TokenPricing(
        input_usd_per_million=62.50 / CREDITS_PER_USD,
        cached_input_usd_per_million=6.250 / CREDITS_PER_USD,
        output_usd_per_million=375.0 / CREDITS_PER_USD,
        source="openai_codex_token_rate_card_credits_converted_to_usd",
    ),
    "gpt-5.4-mini": TokenPricing(
        input_usd_per_million=18.75 / CREDITS_PER_USD,
        cached_input_usd_per_million=1.875 / CREDITS_PER_USD,
        output_usd_per_million=113.0 / CREDITS_PER_USD,
        source="openai_codex_token_rate_card_credits_converted_to_usd",
    ),
    "gpt-5.3-codex": TokenPricing(
        input_usd_per_million=43.75 / CREDITS_PER_USD,
        cached_input_usd_per_million=4.375 / CREDITS_PER_USD,
        output_usd_per_million=350.0 / CREDITS_PER_USD,
        source="openai_codex_token_rate_card_credits_converted_to_usd",
    ),
}


API_STANDARD_USD_PRICING: dict[str, TokenPricing] = {
    "gpt-5.5": TokenPricing(
        input_usd_per_million=5.00,
        cached_input_usd_per_million=0.50,
        output_usd_per_million=30.00,
        source="openai_api_standard_pricing",
    ),
    "gpt-5.5-pro": TokenPricing(
        input_usd_per_million=30.00,
        cached_input_usd_per_million=None,
        output_usd_per_million=180.00,
        source="openai_api_standard_pricing",
    ),
    "gpt-5.4": TokenPricing(
        input_usd_per_million=2.50,
        cached_input_usd_per_million=0.25,
        output_usd_per_million=15.00,
        source="openai_api_standard_short_context_pricing",
    ),
    "gpt-5.4-mini": TokenPricing(
        input_usd_per_million=0.75,
        cached_input_usd_per_million=0.075,
        output_usd_per_million=4.50,
        source="openai_api_standard_pricing",
    ),
    "gpt-5.4-nano": TokenPricing(
        input_usd_per_million=0.20,
        cached_input_usd_per_million=0.02,
        output_usd_per_million=1.25,
        source="openai_api_standard_pricing",
    ),
    "gpt-5.4-pro": TokenPricing(
        input_usd_per_million=30.00,
        cached_input_usd_per_million=None,
        output_usd_per_million=180.00,
        source="openai_api_standard_short_context_pricing",
    ),
    "gpt-5.3-codex": TokenPricing(
        input_usd_per_million=1.75,
        cached_input_usd_per_million=0.175,
        output_usd_per_million=14.00,
        source="openai_api_standard_pricing",
    ),
    "gpt-5.3-codex-spark": TokenPricing(
        input_usd_per_million=1.75,
        cached_input_usd_per_million=0.175,
        output_usd_per_million=14.00,
        source="openai_api_gpt_5_3_codex_pricing_applied_to_codex_spark_preview",
        precise=False,
    ),
}


API_PROVIDER_USD_PRICING: dict[str, dict[str, TokenPricing]] = {
    "gemini": {
        "gemini-3-pro-preview": TokenPricing(
            input_usd_per_million=2.00,
            cached_input_usd_per_million=None,
            output_usd_per_million=12.00,
            source="gemini_api_standard_paid_tier_pricing",
        ),
        "gemini-3-flash-preview": TokenPricing(
            input_usd_per_million=0.50,
            cached_input_usd_per_million=0.05,
            output_usd_per_million=3.00,
            source="gemini_api_standard_paid_tier_pricing",
        ),
        "gemini-3.1-pro-preview": TokenPricing(
            input_usd_per_million=2.00,
            cached_input_usd_per_million=None,
            output_usd_per_million=12.00,
            source="gemini_api_standard_paid_tier_pricing",
        ),
        "gemini-3.1-flash-lite-preview": TokenPricing(
            input_usd_per_million=0.10,
            cached_input_usd_per_million=0.025,
            output_usd_per_million=0.40,
            source="gemini_api_standard_paid_tier_pricing",
        ),
        "gemma-3-27b-it": TokenPricing(
            input_usd_per_million=0.0,
            cached_input_usd_per_million=0.0,
            output_usd_per_million=0.0,
            source="gemini_api_gemma_3_free_pricing",
        ),
    },
    "deepseek": {
        "deepseek-v4-flash": TokenPricing(
            input_usd_per_million=0.14,
            cached_input_usd_per_million=0.028,
            output_usd_per_million=0.28,
            source="deepseek_api_models_pricing",
        ),
        "deepseek-v4-pro": TokenPricing(
            input_usd_per_million=1.74,
            cached_input_usd_per_million=0.145,
            output_usd_per_million=3.48,
            source="deepseek_api_models_pricing",
        ),
    },
    "claude": {
        "claude-opus-4-7": TokenPricing(
            input_usd_per_million=5.00,
            cached_input_usd_per_million=0.50,
            output_usd_per_million=25.00,
            source="anthropic_claude_opus_4_7_pricing",
        ),
        "claude-sonnet-4-6": TokenPricing(
            input_usd_per_million=3.00,
            cached_input_usd_per_million=0.30,
            output_usd_per_million=15.00,
            source="anthropic_claude_sonnet_4_6_pricing",
        ),
    },
    "openrouter": {
        "inclusionai/ling-2.6-1t:free": TokenPricing(
            input_usd_per_million=0.0,
            cached_input_usd_per_million=0.0,
            output_usd_per_million=0.0,
            source="openrouter_free_model_pricing",
        ),
        "google/gemma-4-26b-a4b-it:free": TokenPricing(
            input_usd_per_million=0.0,
            cached_input_usd_per_million=0.0,
            output_usd_per_million=0.0,
            source="openrouter_free_model_pricing",
        ),
        "google/gemma-4-31b-it:free": TokenPricing(
            input_usd_per_million=0.0,
            cached_input_usd_per_million=0.0,
            output_usd_per_million=0.0,
            source="openrouter_free_model_pricing",
        ),
        "tencent/hy3-preview:free": TokenPricing(
            input_usd_per_million=0.0,
            cached_input_usd_per_million=0.0,
            output_usd_per_million=0.0,
            source="openrouter_free_model_pricing",
        ),
    },
    "vercel": {
        "alibaba/qwen3.6-27b": TokenPricing(
            input_usd_per_million=0.60,
            cached_input_usd_per_million=None,
            output_usd_per_million=3.60,
            source="vercel_ai_gateway_qwen3_6_27b_pricing",
        ),
        "alibaba/qwen-3.6-max-preview": TokenPricing(
            input_usd_per_million=1.30,
            cached_input_usd_per_million=0.26,
            output_usd_per_million=7.80,
            source="vercel_ai_gateway_qwen3_6_max_preview_pricing",
        ),
    },
}


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _first_number(payload: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (int, float)):
            return max(0, int(value))
    return 0


def _nested_number(payload: dict[str, Any], key: str, *nested_keys: str) -> int:
    nested = payload.get(key)
    if not isinstance(nested, dict):
        return 0
    return _first_number(nested, *nested_keys)


def normalize_usage(usage: dict[str, Any] | None) -> TokenUsage:
    if not isinstance(usage, dict):
        return TokenUsage()
    input_tokens = _first_number(
        usage,
        "input_tokens",
        "inputTokens",
        "input",
        "prompt_tokens",
        "promptTokenCount",
    )
    runtime_cache_read_tokens = _first_number(usage, "cacheReadTokens", "cacheRead")
    runtime_cache_write_tokens = _first_number(
        usage, "cacheWriteTokens", "cacheWrite"
    )
    cached_input_tokens = _first_number(
        usage,
        "cached_input_tokens",
        "cachedInputTokens",
        "cacheReadTokens",
        "cacheRead",
        "cached_tokens",
        "cache_read_input_tokens",
    )
    cached_input_tokens += _nested_number(
        usage,
        "input_tokens_details",
        "cached_tokens",
        "cached_input_tokens",
    )
    if (
        ("input" in usage or "output" in usage)
        and "input_tokens" not in usage
        and "inputTokens" not in usage
    ):
        input_tokens += runtime_cache_read_tokens + runtime_cache_write_tokens
    output_tokens = _first_number(
        usage,
        "output_tokens",
        "outputTokens",
        "output",
        "completion_tokens",
        "candidatesTokenCount",
    )
    output_tokens += _first_number(usage, "thoughtsTokenCount", "thoughts_tokens")
    total_tokens = _first_number(
        usage, "total_tokens", "totalTokens", "total", "totalTokenCount"
    )
    if not total_tokens:
        total_tokens = input_tokens + output_tokens
    cached_input_tokens = (
        min(cached_input_tokens, input_tokens) if input_tokens else cached_input_tokens
    )
    return TokenUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _canonical_model(model: str) -> str:
    model_name = str(model or "").strip().lower()
    for suffix in (" xhigh", " high", " medium", " low"):
        if model_name.endswith(suffix):
            model_name = model_name[: -len(suffix)]
            break
    return model_name


def pricing_for(provider: str, model: str) -> TokenPricing | None:
    model_name = _canonical_model(model)
    provider_name = str(provider or "").strip().lower()
    if provider_name == "openai_codex":
        return API_STANDARD_USD_PRICING.get(model_name)
    if provider_name in {"codex", "pi"}:
        return CODEX_SUBSCRIPTION_USD_PRICING.get(model_name)
    if provider_name == "openai":
        return API_STANDARD_USD_PRICING.get(model_name)
    provider_pricing = API_PROVIDER_USD_PRICING.get(provider_name)
    if provider_pricing is not None:
        return provider_pricing.get(model_name)
    return None


def estimate_token_cost_usd(provider: str, model: str, usage: TokenUsage) -> tuple[float, TokenPricing | None]:
    pricing = pricing_for(provider, model)
    if pricing is None:
        return 0.0, None
    provider_name = str(provider or "").strip().lower()
    model_name = _canonical_model(model)
    if provider_name == "gemini" and model_name in {
        "gemini-3-pro-preview",
        "gemini-3.1-pro-preview",
    } and usage.input_tokens > 200_000:
        pricing = TokenPricing(
            input_usd_per_million=4.00,
            cached_input_usd_per_million=None,
            output_usd_per_million=18.00,
            source="gemini_api_standard_paid_tier_long_context_pricing",
        )
    uncached_input_tokens = max(0, usage.input_tokens - usage.cached_input_tokens)
    cached_rate = pricing.cached_input_usd_per_million
    cached_input_cost = 0.0
    if cached_rate is None:
        uncached_input_tokens += usage.cached_input_tokens
    else:
        cached_input_cost = usage.cached_input_tokens * cached_rate / TOKENS_PER_MILLION
    cost = (
        uncached_input_tokens * pricing.input_usd_per_million / TOKENS_PER_MILLION
        + cached_input_cost
        + usage.output_tokens * pricing.output_usd_per_million / TOKENS_PER_MILLION
    )
    return cost, pricing


def _tool_cost_usd(provider: str, usage: dict[str, Any] | None) -> float:
    if str(provider or "").strip().lower() not in {"openai", "openai_codex"} or not isinstance(usage, dict):
        return 0.0
    web_search_calls = _first_number(usage, "web_search_calls", "webSearchCalls")
    code_interpreter_sessions = _first_number(
        usage,
        "code_interpreter_sessions",
        "codeInterpreterSessions",
    )
    return (
        web_search_calls * WEB_SEARCH_USD_PER_1K_CALLS / 1000.0
        + code_interpreter_sessions * CODE_INTERPRETER_USD_PER_SESSION
    )


def _has_explicit_cached_input_breakdown(usage: dict[str, Any] | None) -> bool:
    if not isinstance(usage, dict):
        return False
    if any(key in usage for key in ("cached_input_tokens", "cachedInputTokens", "cacheReadTokens", "cacheRead", "cached_tokens")):
        return True
    details = usage.get("input_tokens_details")
    if isinstance(details, dict) and any(key in details for key in ("cached_tokens", "cached_input_tokens")):
        return True
    return False


def _uses_openai_billable_tools(provider: str, access: str = "") -> bool:
    return (
        str(provider or "").strip().lower() in {"openai", "openai_codex"}
        and str(access or "").strip().lower() in {"web", "web_ci"}
    )


def cost_is_precise(
    provider: str, model: str, usage: dict[str, Any] | None, *, access: str = ""
) -> bool:
    pricing = pricing_for(provider, model)
    if pricing is None or not pricing.precise:
        return False
    if _uses_openai_billable_tools(provider, access):
        return False
    if not isinstance(usage, dict):
        return False
    normalized = normalize_usage(usage)
    if not _has_explicit_cached_input_breakdown(usage):
        return False
    if pricing.cached_input_usd_per_million is None and normalized.cached_input_tokens > 0:
        return False
    return True


def usage_with_cost(
    provider: str, model: str, usage: dict[str, Any] | None, *, access: str = ""
) -> dict[str, Any]:
    normalized = normalize_usage(usage)
    cost, pricing = estimate_token_cost_usd(provider, model, normalized)
    tool_cost = _tool_cost_usd(provider, usage)
    cost += tool_cost
    precise = cost_is_precise(provider, model, usage, access=access)
    cost_source = pricing.source if pricing is not None else "unknown_model_pricing"
    if pricing is not None and _uses_openai_billable_tools(provider, access):
        cost_source = (
            f"{pricing.source}_includes_counted_tool_charges"
            if tool_cost
            else f"{pricing.source}_token_only_excludes_uncounted_tool_charges"
        )
    payload = asdict(normalized)
    if isinstance(usage, dict):
        payload["raw_usage"] = dict(usage)
    payload.update(
        {
            "estimated_cost_usd": cost if pricing is not None else None,
            "cost_is_precise": precise,
            "cost_source": cost_source,
            "estimated_tool_cost_usd": tool_cost,
        }
    )
    if pricing is not None:
        payload["pricing"] = asdict(pricing)
    return payload
