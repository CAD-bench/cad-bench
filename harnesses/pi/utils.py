from __future__ import annotations

import json
import subprocess
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from bench.config import env_value
from harnesses.codex.utils import codex_oauth_credentials
from harnesses.utils import (
    AgentRunResult,
    CONTAINER_OFFLINE_SITE_PACKAGES_DIR,
    CONTAINER_PI_DIR,
    PI_WEB_EXTENSION_CONTAINER_PATH,
    REASONING_EFFORTS,
    RUNTIME_HOME_TMP_ROOT,
    build_step_prompt,
    harness,
    offline_agent_container_env,
    offline_agent_site_packages_dir,
    parse_json_events,
    sanitize_command_for_logging,
    web_agent_container_env,
)

PI_PROVIDER_NAME = "codex-runtime"
PI_MODELS = {
    "gpt-5.4": {
        "name": "GPT-5.4",
        "reasoning": True,
        "input": ["text"],
        "contextWindow": 400000,
        "maxTokens": 128000,
    },
    "gpt-5.4-mini": {
        "name": "GPT-5.4 Mini",
        "reasoning": True,
        "input": ["text"],
        "contextWindow": 400000,
        "maxTokens": 128000,
    },
}


def make_harness(model: str, mode: str, level: str):
    return harness(
        f"pi/{model}-{mode}-{level}",
        provider="pi",
        strategy="agent_step",
        model=model,
        reasoning_effort=REASONING_EFFORTS[level],
        access=mode,
    )


build_prompt = build_step_prompt


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(part for part in parts if part).strip()


def _merge_usage_totals(total: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
    merged = dict(total)
    for key, value in usage.items():
        if isinstance(value, dict):
            current = merged.get(key)
            if isinstance(current, dict):
                merged[key] = _merge_usage_totals(current, value)
            else:
                merged[key] = _merge_usage_totals({}, value)
            continue
        if isinstance(value, (int, float)):
            merged[key] = float(merged.get(key, 0) or 0) + float(value)
            if isinstance(value, int) and float(merged[key]).is_integer():
                merged[key] = int(merged[key])
            continue
        if key not in merged:
            merged[key] = value
    return merged


def _parse_result(stdout: str) -> tuple[str, str, dict[str, Any], list[dict[str, Any]]]:
    events = parse_json_events(stdout)
    session_id = ""
    response = ""
    usage_from_turns: dict[str, Any] = {}
    fallback_usage: dict[str, Any] = {}
    for event in events:
        if event.get("type") == "session":
            session_id = str(event.get("id", ""))
        event_type = str(event.get("type") or "")
        if event_type not in {"message_end", "turn_end", "agent_end"}:
            continue
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        text = _message_text(message)
        if text:
            response = text
        event_usage = message.get("usage")
        if not isinstance(event_usage, dict):
            continue
        if event_type == "turn_end":
            usage_from_turns = _merge_usage_totals(usage_from_turns, event_usage)
        elif not usage_from_turns:
            fallback_usage = _merge_usage_totals(fallback_usage, event_usage)
    return response, session_id, (usage_from_turns or fallback_usage), events


def write_pi_runtime_home(
    target_dir: Path,
    *,
    models: list[dict[str, Any]],
    provider_name: str = PI_PROVIDER_NAME,
) -> Path:
    credentials = codex_oauth_credentials()
    agent_dir = target_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    models_payload = {
        "providers": {
            provider_name: {
                "api": "openai-codex-responses",
                "baseUrl": "https://chatgpt.com/backend-api",
                "apiKey": credentials["access"],
                "models": models,
            }
        }
    }
    (agent_dir / "models.json").write_text(
        json.dumps(models_payload, indent=2) + "\n", encoding="utf-8"
    )
    (target_dir / "web-search.json").write_text(
        json.dumps({"provider": "exa"}, indent=2) + "\n", encoding="utf-8"
    )
    return target_dir


def _cleanup_runtime_home(temp_dir: Path) -> None:
    proc = subprocess.run(
        ["rm", "-rf", str(temp_dir)],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0 and not temp_dir.exists():
        return
    image = env_value("CAD_BENCH_AGENT_IMAGE")
    docker_detail = ""
    if image and temp_dir.exists():
        docker_proc = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--mount",
                f"type=bind,src={temp_dir.parent.resolve()},dst=/cleanup",
                "--entrypoint",
                "rm",
                image,
                "-rf",
                f"/cleanup/{temp_dir.name}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if docker_proc.returncode == 0 and not temp_dir.exists():
            return
        docker_detail = (
            docker_proc.stderr.strip()
            or docker_proc.stdout.strip()
            or "docker cleanup failed"
        )
    host_detail = proc.stderr.strip() or proc.stdout.strip() or "host cleanup failed"
    detail = host_detail if not docker_detail else f"{host_detail} | {docker_detail}"
    raise RuntimeError(f"failed to remove runtime home {temp_dir}: {detail}")


@contextmanager
def pi_runtime_home(
    models: list[dict[str, Any]],
    *,
    provider_name: str = PI_PROVIDER_NAME,
):
    RUNTIME_HOME_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(
        tempfile.mkdtemp(prefix="cad_bench_pi_", dir=str(RUNTIME_HOME_TMP_ROOT))
    )
    with ExitStack() as stack:
        stack.callback(_cleanup_runtime_home, temp_dir)
        yield write_pi_runtime_home(
            temp_dir / ".pi",
            models=models,
            provider_name=provider_name,
        )


def run(
    *,
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
    del system_prompt
    if submission_dir is None:
        raise ValueError("submission_dir is required for step harnesses")
    step_path = (submission_dir / "final.step").resolve()
    step_path.touch(exist_ok=True)
    model_name = str(spec.model)
    model_config = {"id": model_name, **PI_MODELS.get(model_name, {})}
    with pi_runtime_home([model_config]) as auth_home:
        mounts = [
            runtime.BindMount(workdir.resolve(), runtime.CONTAINER_WORKDIR),
            runtime.BindMount(
                submission_dir.resolve(), runtime.CONTAINER_SUBMISSION_DIR
            ),
            runtime.BindMount(step_path, runtime.CONTAINER_HOME_STEP_PATH),
            runtime.BindMount(auth_home.resolve(), CONTAINER_PI_DIR),
        ]
        env: dict[str, str] = web_agent_container_env()
        if docs_dir is not None:
            mounts.append(
                runtime.BindMount(
                    docs_dir.resolve(), runtime.CONTAINER_DOCS_DIR, read_only=True
                )
            )
        if str(spec.access) == "offline":
            mounts.append(
                runtime.BindMount(
                    offline_agent_site_packages_dir(),
                    CONTAINER_OFFLINE_SITE_PACKAGES_DIR,
                    read_only=True,
                )
            )
            env = offline_agent_container_env()
        exa_api_key = env_value("EXA_API_KEY")
        if str(spec.access) == "web" and exa_api_key:
            env["EXA_API_KEY"] = exa_api_key
        args = [
            "--provider",
            PI_PROVIDER_NAME,
            "--model",
            model_name,
            "--mode",
            "json",
            "--thinking",
            str(spec.reasoning_effort),
            "--no-session",
            "--print",
        ]
        if str(spec.access) == "web":
            args.extend(["--extension", PI_WEB_EXTENSION_CONTAINER_PATH])
        else:
            args.append("--no-extensions")
        args.extend(["--tools", "read,bash,edit,write,grep,find,ls", prompt])
        cmd = runtime.docker_run_command(
            image=image,
            entrypoint="pi",
            mounts=mounts,
            env=env,
            args=args,
            remove=container_export_path is None,
            container_name=container_name,
            network="none" if str(spec.access) == "offline" else "bridge",
        )
        proc = runtime.run_docker_command(
            image=image,
            entrypoint="pi",
            mounts=mounts,
            env=env,
            args=args,
            container_export_path=container_export_path,
            container_name=container_name,
            network="none" if str(spec.access) == "offline" else "bridge",
        )
    response, session_id, usage, events = _parse_result(proc.stdout)
    safe_cmd = sanitize_command_for_logging(cmd)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        return AgentRunResult(
            response,
            f"pi exit {proc.returncode}: {detail}",
            proc.stdout,
            proc.stderr,
            proc.returncode,
            safe_cmd,
            session_id,
            usage,
            events,
        )
    if not events:
        return AgentRunResult(
            response,
            "pi did not emit any JSON events",
            proc.stdout,
            proc.stderr,
            proc.returncode,
            safe_cmd,
            session_id,
            usage,
            events,
        )
    if not usage:
        return AgentRunResult(
            response,
            "pi did not emit usage data",
            proc.stdout,
            proc.stderr,
            proc.returncode,
            safe_cmd,
            session_id,
            usage,
            events,
        )
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
