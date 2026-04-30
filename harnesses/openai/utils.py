from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from bench.config import required_env_value

from harnesses.utils import (
    AgentRunResult,
    REASONING_EFFORTS,
    XML_CODE_SYSTEM_PROMPT,
    build_single_turn_prompt,
    harness,
    sanitize_command_for_logging,
)


def make_harness(model: str, mode: str, level: str):
    return harness(
        f"openai/{model}-{mode}-{level}",
        provider="openai",
        strategy="one_shot_code",
        model=model,
        reasoning_effort=REASONING_EFFORTS[level],
        access=mode,
        system_prompt=XML_CODE_SYSTEM_PROMPT,
    )


def build_prompt(spec: Any, task_prompt: str) -> tuple[str | None, str]:
    return build_single_turn_prompt(spec, task_prompt)


def _parse_result(
    stdout: str,
) -> tuple[str, str, dict[str, Any], list[dict[str, Any]], str]:
    payload = json.loads(stdout.strip() or "{}")
    if not isinstance(payload, dict):
        raise TypeError("OpenAI harness stdout must be a JSON object")
    response = payload.get("response", "")
    response_id = payload.get("response_id", "")
    usage = payload.get("usage", {})
    raw_response = payload.get("raw_response")
    events = [raw_response] if isinstance(raw_response, dict) else []
    status_history_path = payload.get("status_history_path", "")
    return (
        str(response),
        str(response_id),
        dict(usage) if isinstance(usage, dict) else {},
        events,
        str(status_history_path),
    )


def _tool_usage_from_response(raw_response: dict[str, Any]) -> dict[str, int]:
    output = raw_response.get("output")
    if not isinstance(output, list):
        return {}
    web_search_calls = 0
    code_interpreter_calls = 0
    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type == "web_search_call":
            web_search_calls += 1
        elif item_type == "code_interpreter_call":
            code_interpreter_calls += 1
    usage: dict[str, int] = {}
    if web_search_calls:
        usage["web_search_calls"] = web_search_calls
    if code_interpreter_calls:
        usage["code_interpreter_sessions"] = 1
        usage["code_interpreter_calls"] = code_interpreter_calls
    return usage


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
    del submission_dir, docs_dir
    env = {"OPENAI_API_KEY": required_env_value("OPENAI_API_KEY")}
    mounts = [runtime.BindMount(workdir.resolve(), runtime.CONTAINER_WORKDIR)]
    config_file = workdir / ".openai_request.json"
    with ExitStack() as stack:
        stack.callback(lambda: config_file.exists() and config_file.unlink())
        config_file.write_text(
            json.dumps(
                {
                    "model": str(spec.model),
                    "access_mode": str(spec.access),
                    "reasoning_effort": str(spec.reasoning_effort),
                    "timeout_seconds": 72 * 60 * 60,
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
            "harnesses/openai/runner.py",
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
    status_artifacts: dict[str, str] = {}
    if proc.returncode == 0:
        _, _, _, _, status_history_path = _parse_result(proc.stdout)
        if status_history_path:
            status_path = workdir / status_history_path
            if status_path.exists():
                status_artifacts["openai_response_status.jsonl"] = status_path.read_text(
                    encoding="utf-8", errors="ignore"
                )
    safe_cmd = sanitize_command_for_logging(cmd)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        return AgentRunResult(
            "",
            f"openai exit {proc.returncode}: {detail}",
            proc.stdout,
            proc.stderr,
            proc.returncode,
            safe_cmd,
            "",
            {},
            [],
            status_artifacts,
        )
    response, session_id, usage, events, _ = _parse_result(proc.stdout)
    raw_response = events[0] if events and isinstance(events[0], dict) else {}
    usage = {**usage, **_tool_usage_from_response(raw_response)}
    status = str(raw_response.get("status", "")).strip()
    if status and status != "completed":
        return AgentRunResult(
            response,
            f"openai response ended with status={status}",
            proc.stdout,
            proc.stderr,
            proc.returncode,
            safe_cmd,
            session_id,
            usage,
            events,
            status_artifacts,
        )
    if not usage:
        return AgentRunResult(
            response,
            "openai response is missing usage data",
            proc.stdout,
            proc.stderr,
            proc.returncode,
            safe_cmd,
            session_id,
            usage,
            events,
            status_artifacts,
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
        status_artifacts,
    )
