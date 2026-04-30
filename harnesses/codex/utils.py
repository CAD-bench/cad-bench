from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from bench.config import env_b64_json_value, env_value, set_env_b64_json_value

from harnesses.utils import (
    AgentRunResult,
    CONTAINER_CODEX_DIR,
    CONTAINER_OFFLINE_SITE_PACKAGES_DIR,
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

CODEX_AUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_AUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_JWT_CLAIM_PATH = "https://api.openai.com/auth"


def make_harness(model: str, mode: str, level: str):
    return harness(
        f"codex/{model}-{mode}-{level}",
        provider="codex",
        strategy="agent_step",
        model=model,
        reasoning_effort=REASONING_EFFORTS[level],
        access=mode,
    )


build_prompt = build_step_prompt


def _parse_result(stdout: str) -> tuple[str, str, dict[str, Any], list[dict[str, Any]]]:
    events = parse_json_events(stdout)
    thread_id = ""
    response = ""
    usage: dict[str, Any] = {}
    for event in events:
        if event.get("type") == "thread.started":
            thread_id = str(event.get("thread_id", ""))
        if event.get("type") == "item.completed":
            item = event.get("item")
            if (
                isinstance(item, dict)
                and item.get("type") == "agent_message"
                and isinstance(item.get("text"), str)
            ):
                response = item["text"].strip()
        if event.get("type") == "turn.completed" and isinstance(
            event.get("usage"), dict
        ):
            usage = dict(event["usage"])
    return response, thread_id, usage, events


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("invalid JWT")
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    decoded = base64.urlsafe_b64decode(payload + padding)
    value = json.loads(decoded.decode("utf-8"))
    if not isinstance(value, dict):
        raise TypeError("JWT payload must decode to an object")
    return value


def _codex_tokens(payload: dict[str, Any]) -> dict[str, Any]:
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        raise ValueError("Codex auth payload is missing tokens")
    access = str(tokens.get("access_token", "")).strip()
    refresh = str(tokens.get("refresh_token", "")).strip()
    if not access or not refresh:
        raise ValueError("Codex auth payload is missing access or refresh token")
    return tokens


def has_codex_auth_token() -> bool:
    try:
        payload = env_b64_json_value("CODEX_AUTH_JSON_B64")
        _codex_tokens(payload)
    except Exception:
        return False
    return True


def _codex_access_token_expired(
    payload: dict[str, Any], buffer_seconds: int = 300
) -> bool:
    access = str(_codex_tokens(payload)["access_token"])
    token_payload = _decode_jwt_payload(access)
    expires_at = int(token_payload.get("exp", 0))
    return expires_at <= int(time.time()) + buffer_seconds


def _refresh_codex_auth_payload(payload: dict[str, Any]) -> dict[str, Any]:
    refresh_token = str(_codex_tokens(payload)["refresh_token"])
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CODEX_AUTH_CLIENT_ID,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        CODEX_AUTH_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        refreshed = json.load(response)
    if not isinstance(refreshed, dict):
        raise TypeError("Codex refresh response must be a JSON object")
    access = str(refreshed.get("access_token", "")).strip()
    refresh = str(refreshed.get("refresh_token", "")).strip()
    if not access or not refresh:
        raise ValueError("Codex refresh response is missing access or refresh token")
    next_payload = dict(payload)
    next_tokens = dict(_codex_tokens(payload))
    next_tokens["access_token"] = access
    next_tokens["refresh_token"] = refresh
    next_payload["tokens"] = next_tokens
    next_payload["last_refresh"] = int(time.time())
    set_env_b64_json_value("CODEX_AUTH_JSON_B64", next_payload)
    return next_payload


def current_codex_auth_payload(*, refresh_buffer_seconds: int = 3600) -> dict[str, Any]:
    payload = env_b64_json_value("CODEX_AUTH_JSON_B64")
    if _codex_access_token_expired(payload, buffer_seconds=refresh_buffer_seconds):
        payload = _refresh_codex_auth_payload(payload)
    return payload


def codex_oauth_credentials() -> dict[str, Any]:
    payload = current_codex_auth_payload()
    tokens = _codex_tokens(payload)
    access = str(tokens["access_token"])
    refresh = str(tokens["refresh_token"])
    token_payload = _decode_jwt_payload(access)
    auth_claim = token_payload.get(CODEX_JWT_CLAIM_PATH)
    if not isinstance(auth_claim, dict):
        raise ValueError("Codex access token is missing auth claim")
    account_id = str(auth_claim.get("chatgpt_account_id", "")).strip()
    if not account_id:
        raise ValueError("Codex access token is missing chatgpt account id")
    expires_at = int(token_payload.get("exp", 0)) * 1000
    if expires_at <= 0:
        raise ValueError("Codex access token is missing exp")
    return {
        "access": access,
        "refresh": refresh,
        "accountId": account_id,
        "expires": expires_at,
    }


def local_codex_oauth_credentials() -> dict[str, Any]:
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    auth_path = codex_home / "auth.json"
    if not auth_path.exists():
        raise FileNotFoundError(f"Codex auth file not found: {auth_path}")
    payload = json.loads(auth_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("Codex auth file must contain a JSON object")
    if payload.get("auth_mode") != "chatgpt":
        raise ValueError("Codex auth file must use auth_mode=chatgpt")
    tokens = _codex_tokens(payload)
    access = str(tokens["access_token"])
    refresh = str(tokens["refresh_token"])
    token_payload = _decode_jwt_payload(access)
    auth_claim = token_payload.get(CODEX_JWT_CLAIM_PATH)
    account_id = str(tokens.get("account_id", "")).strip()
    if not account_id and isinstance(auth_claim, dict):
        account_id = str(auth_claim.get("chatgpt_account_id", "")).strip()
    if not account_id:
        raise ValueError("Codex auth file is missing chatgpt account id")
    expires_at = int(token_payload.get("exp", 0)) * 1000
    if expires_at <= 0:
        raise ValueError("Codex auth file access token is missing exp")
    return {
        "access": access,
        "refresh": refresh,
        "accountId": account_id,
        "expires": expires_at,
    }


def write_codex_runtime_home(target_dir: Path) -> Path:
    payload = current_codex_auth_payload()
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "auth.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    config_toml = env_value("CODEX_CONFIG_TOML")
    if config_toml:
        (target_dir / "config.toml").write_text(config_toml + "\n", encoding="utf-8")
    return target_dir


def sync_codex_runtime_auth(runtime_codex_dir: Path) -> None:
    runtime_auth = runtime_codex_dir / "auth.json"
    if not runtime_auth.exists():
        return
    payload = json.loads(runtime_auth.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("runtime Codex auth payload must be a JSON object")
    _codex_tokens(payload)
    set_env_b64_json_value("CODEX_AUTH_JSON_B64", payload)


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
def codex_runtime_home():
    RUNTIME_HOME_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(
        tempfile.mkdtemp(prefix="cad_bench_codex_", dir=str(RUNTIME_HOME_TMP_ROOT))
    )
    with ExitStack() as stack:
        stack.callback(_cleanup_runtime_home, temp_dir)
        yield write_codex_runtime_home(temp_dir / ".codex")


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
    with codex_runtime_home() as auth_home:
        mounts = [
            runtime.BindMount(workdir.resolve(), runtime.CONTAINER_WORKDIR),
            runtime.BindMount(
                submission_dir.resolve(), runtime.CONTAINER_SUBMISSION_DIR
            ),
            runtime.BindMount(step_path, runtime.CONTAINER_HOME_STEP_PATH),
            runtime.BindMount(auth_home.resolve(), CONTAINER_CODEX_DIR),
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
        args = [
            "--dangerously-bypass-approvals-and-sandbox",
            "--enable",
            "multi_agent",
        ]
        if str(spec.access) == "web":
            args.append("--search")
        args.extend(
            [
                "exec",
                "--json",
                "-m",
                str(spec.model),
                "--skip-git-repo-check",
                "--ephemeral",
                "-C",
                runtime.CONTAINER_WORKDIR,
                "-c",
                'model_reasoning_summary="auto"',
            ]
        )
        if str(spec.reasoning_effort).strip():
            args.extend(["-c", f'model_reasoning_effort="{spec.reasoning_effort}"'])
        args.append(prompt)
        cmd = runtime.docker_run_command(
            image=image,
            entrypoint="codex",
            mounts=mounts,
            env=env,
            args=args,
            remove=container_export_path is None,
            container_name=container_name,
            network="none" if str(spec.access) == "offline" else "bridge",
        )
        proc = runtime.run_docker_command(
            image=image,
            entrypoint="codex",
            mounts=mounts,
            env=env,
            args=args,
            container_export_path=container_export_path,
            container_name=container_name,
            network="none" if str(spec.access) == "offline" else "bridge",
        )
        sync_codex_runtime_auth(auth_home)
    response, session_id, usage, events = _parse_result(proc.stdout)
    safe_cmd = sanitize_command_for_logging(cmd)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        return AgentRunResult(
            response,
            f"codex exit {proc.returncode}: {detail}",
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
            "codex did not emit any JSON events",
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
            "codex did not emit usage data",
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
