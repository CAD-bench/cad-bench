from __future__ import annotations

import json
import os
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bench.config import env_value

from harnesses.utils import (
    AgentRunResult,
    XML_CODE_SYSTEM_PROMPT,
    build_single_turn_prompt,
    harness,
    sanitize_command_for_logging,
)


@dataclass(frozen=True)
class ProviderConfig:
    kind: str
    api_key_env_names: tuple[str, ...]
    base_url_env_name: str
    default_base_url: str
    default_max_tokens: int | None = None
    use_codex_oauth: bool = False


PROVIDER_CONFIGS: dict[str, ProviderConfig] = {
    "openai_codex": ProviderConfig(
        kind="openai_codex_responses",
        api_key_env_names=(),
        base_url_env_name="OPENAI_CODEX_BASE_URL",
        default_base_url="https://chatgpt.com/backend-api/codex",
        use_codex_oauth=True,
    ),
    "kimi": ProviderConfig(
        kind="openai_compatible",
        api_key_env_names=("KIMI_API_KEY", "MOONSHOT_API_KEY"),
        base_url_env_name="KIMI_BASE_URL",
        default_base_url="https://api.moonshot.ai/v1",
    ),
    "deepseek": ProviderConfig(
        kind="openai_compatible",
        api_key_env_names=("DEEPSEEK_API_KEY",),
        base_url_env_name="DEEPSEEK_BASE_URL",
        default_base_url="https://api.deepseek.com",
    ),
    "openrouter": ProviderConfig(
        kind="openai_compatible",
        api_key_env_names=("OPENROUTER_API_KEY",),
        base_url_env_name="OPENROUTER_BASE_URL",
        default_base_url="https://openrouter.ai/api/v1",
    ),
    "vercel": ProviderConfig(
        kind="openai_compatible",
        api_key_env_names=("AI_GATEWAY_API_KEY", "VERCEL_AI_GATEWAY_API_KEY"),
        base_url_env_name="AI_GATEWAY_BASE_URL",
        default_base_url="https://ai-gateway.vercel.sh/v1",
    ),
    "gemini": ProviderConfig(
        kind="gemini",
        api_key_env_names=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        base_url_env_name="GEMINI_BASE_URL",
        default_base_url="https://generativelanguage.googleapis.com/v1beta",
    ),
    "claude": ProviderConfig(
        kind="anthropic",
        api_key_env_names=("ANTHROPIC_API_KEY",),
        base_url_env_name="ANTHROPIC_BASE_URL",
        default_base_url="https://api.anthropic.com/v1",
        default_max_tokens=64_000,
    ),
}


def api_key_env_names(provider: str) -> tuple[str, ...]:
    return _provider_config(provider).api_key_env_names


def env_value_with_process_fallback(name: str, default: str = "") -> str:
    value = env_value(name)
    if value:
        return value
    return str(os.environ.get(name, default)).strip()


def make_harness(provider: str, model: str, mode: str, level: str):
    if mode not in {"offline", "offline_nodocs"}:
        raise ValueError(
            f"{provider} API harnesses only support offline or offline_nodocs mode"
        )
    if level != "none":
        raise ValueError(f"{provider} API harnesses only support level=none")
    system_prompt = XML_CODE_SYSTEM_PROMPT
    if provider == "gemini" and model.startswith("gemma-"):
        system_prompt = None
    return harness(
        f"{provider}/{model}-{mode}-{level}",
        provider=provider,
        strategy="one_shot_code",
        model=model,
        reasoning_effort="",
        access=mode,
        system_prompt=system_prompt,
    )


def build_prompt(spec: Any, task_prompt: str) -> tuple[str | None, str]:
    return build_single_turn_prompt(spec, task_prompt)


def _provider_config(provider: str) -> ProviderConfig:
    provider_name = str(provider).strip().lower()
    config = PROVIDER_CONFIGS.get(provider_name)
    if config is None:
        raise ValueError(f"unsupported API model provider: {provider}")
    return config


def _first_required_env(names: tuple[str, ...]) -> tuple[str, str]:
    if not names:
        raise ValueError("api key env names must not be empty")
    for name in names:
        value = env_value_with_process_fallback(name)
        if value:
            return name, value
    joined = " or ".join(names)
    raise RuntimeError(f"{joined} is required in .env or the process environment")


def _codex_oauth_credentials() -> dict[str, Any]:
    from harnesses.codex.utils import codex_oauth_credentials, local_codex_oauth_credentials

    try:
        return codex_oauth_credentials()
    except Exception:
        return local_codex_oauth_credentials()


def _parse_result(stdout: str) -> tuple[str, str, dict[str, Any], list[dict[str, Any]]]:
    payload = json.loads(stdout.strip() or "{}")
    if not isinstance(payload, dict):
        raise TypeError("API model harness stdout must be a JSON object")
    response = payload.get("response", "")
    response_id = payload.get("response_id", "")
    usage = payload.get("usage", {})
    raw_response = payload.get("raw_response")
    events = [raw_response] if isinstance(raw_response, dict) else []
    return (
        str(response),
        str(response_id),
        dict(usage) if isinstance(usage, dict) else {},
        events,
    )


def run(
    *,
    provider: str,
    runtime: Any,
    spec: Any,
    system_prompt: str | None,
    prompt: str,
    workdir: Path,
    submission_dir: Path | None,
    image: str,
    docs_dir: Path | None = None,
    container_export_path: Path | None = None,
    container_name: str = "",
) -> AgentRunResult:
    del submission_dir, docs_dir
    provider_name = str(provider).strip().lower()
    config = _provider_config(provider_name)
    account_id = ""
    if config.use_codex_oauth:
        credentials = _codex_oauth_credentials()
        api_key_env_name = "CODEX_AUTH_JSON_B64"
        api_key = str(credentials["access"])
        account_id = str(credentials.get("accountId", ""))
    else:
        api_key_env_name, api_key = _first_required_env(config.api_key_env_names)
    base_url = env_value_with_process_fallback(
        config.base_url_env_name, config.default_base_url
    )
    env = {"CAD_BENCH_API_KEY": api_key}
    api_max_attempts = env_value_with_process_fallback("CAD_BENCH_API_MAX_ATTEMPTS")
    if api_max_attempts:
        env["CAD_BENCH_API_MAX_ATTEMPTS"] = api_max_attempts
    mounts = [runtime.BindMount(workdir.resolve(), runtime.CONTAINER_WORKDIR)]
    config_file = workdir / f".{provider_name}_request.json"
    with ExitStack() as stack:
        stack.callback(lambda: config_file.exists() and config_file.unlink())
        config_file.write_text(
            json.dumps(
                {
                    "provider": provider_name,
                    "kind": config.kind,
                    "model": str(spec.model),
                    "base_url": base_url,
                    "api_key_env_name": api_key_env_name,
                    "account_id": account_id,
                    "max_tokens": config.default_max_tokens,
                    "reasoning_effort": str(getattr(spec, "reasoning_effort", "")),
                    "prompt": prompt,
                    "system_prompt": system_prompt or "",
                }
            ),
            encoding="utf-8",
        )
        args = [
            "run",
            "--no-sync",
            "python",
            "harnesses/api_models/runner.py",
            "--config",
            f"{runtime.CONTAINER_WORKDIR}/{config_file.name}",
        ]
        cmd = runtime.docker_run_command(
            image=image,
            entrypoint="uv",
            mounts=mounts,
            env=env,
            args=args,
            workdir=runtime.CONTAINER_APP_DIR,
            remove=container_export_path is None,
            container_name=container_name,
        )
        proc = runtime.run_docker_command(
            image=image,
            entrypoint="uv",
            mounts=mounts,
            env=env,
            args=args,
            workdir=runtime.CONTAINER_APP_DIR,
            container_export_path=container_export_path,
            container_name=container_name,
        )
    safe_cmd = sanitize_command_for_logging(cmd)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"{provider_name} exit {proc.returncode}: {detail}")
    response, session_id, usage, events = _parse_result(proc.stdout)
    if not usage:
        raise RuntimeError(f"{provider_name} response is missing usage data")
    return AgentRunResult(
        response,
        None,
        proc.stdout,
        proc.stderr,
        proc.returncode,
        safe_cmd,
        session_id,
        usage,
        events,
    )
