from __future__ import annotations

import argparse
import json
import os
import re
import sys
from time import sleep, time
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import quote

from openai import OpenAI


TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}


def _retry_delay_seconds(exc: error.HTTPError, attempt: int, detail: str = "") -> float:
    retry_after = exc.headers.get("Retry-After", "").strip()
    if retry_after:
        try:
            return max(1.0, min(120.0, float(retry_after)))
        except ValueError:
            pass
    reset_header = exc.headers.get("X-RateLimit-Reset", "").strip()
    if reset_header:
        try:
            reset_value = float(reset_header)
            if reset_value > 10_000_000_000:
                reset_value /= 1000.0
            return max(1.0, min(120.0, reset_value - time() + 1.0))
        except ValueError:
            pass
    retry_delay_match = re.search(r'"retryDelay"\s*:\s*"([0-9.]+)s"', detail)
    if retry_delay_match:
        return max(1.0, min(120.0, float(retry_delay_match.group(1)) + 1.0))
    retry_in_match = re.search(r"Please retry in ([0-9.]+)s", detail)
    if retry_in_match:
        return max(1.0, min(120.0, float(retry_in_match.group(1)) + 1.0))
    return min(120.0, 8.0 * attempt)


def _max_attempts() -> int:
    raw = os.environ.get("CAD_BENCH_API_MAX_ATTEMPTS", "8").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 8


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    max_attempts = _max_attempts()
    attempt = 0
    while True:
        attempt += 1
        req = request.Request(url, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req) as response:
                raw = response.read().decode("utf-8")
            break
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            can_retry = exc.code in TRANSIENT_HTTP_CODES and (
                max_attempts == 0 or attempt < max_attempts
            )
            if can_retry:
                delay = _retry_delay_seconds(exc, attempt, detail)
                max_attempts_label = "unlimited" if max_attempts == 0 else str(max_attempts)
                print(
                    f"HTTP {exc.code} from provider; retrying in {delay:.1f}s "
                    f"({attempt}/{max_attempts_label}): {detail or exc}",
                    file=sys.stderr,
                )
                sleep(delay)
                continue
            print(detail or str(exc), file=sys.stderr)
            raise SystemExit(1) from exc
        except error.URLError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1) from exc
    loaded = json.loads(raw or "{}")
    if not isinstance(loaded, dict):
        raise TypeError("provider response must be a JSON object")
    return loaded


def _text_from_openai_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts).strip()
    return ""


def _run_openai_compatible(config: dict[str, Any], api_key: str) -> dict[str, Any]:
    base_url = str(config["base_url"]).rstrip("/")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if str(config.get("provider", "")).strip().lower() == "openrouter":
        headers["HTTP-Referer"] = (
            "https://huggingface.co/datasets/CAD-bench/"
            "cad-bench-ed-2026-anonymous-tasks"
        )
        headers["X-Title"] = "CAD Bench"
    raw = _post_json(
        f"{base_url}/chat/completions",
        headers,
        {
            "model": str(config["model"]),
            "messages": [
                {"role": "system", "content": str(config.get("system_prompt", ""))},
                {"role": "user", "content": str(config["prompt"])},
            ],
        },
    )
    choices = raw.get("choices")
    message = {}
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        value = choices[0].get("message")
        if isinstance(value, dict):
            message = value
    response = _text_from_openai_content(message.get("content"))
    return {
        "response": response,
        "response_id": raw.get("id", ""),
        "usage": raw.get("usage") if isinstance(raw.get("usage"), dict) else {},
        "raw_response": raw,
    }


def _extract_response_text(raw_response: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in raw_response.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if (
                isinstance(content, dict)
                and content.get("type") == "output_text"
                and isinstance(content.get("text"), str)
            ):
                parts.append(content["text"])
    return "\n".join(part for part in parts if part).strip()


def _run_openai_codex_responses(
    config: dict[str, Any], api_key: str
) -> dict[str, Any]:
    headers = {}
    account_id = str(config.get("account_id", "")).strip()
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    client = OpenAI(
        api_key=api_key,
        base_url=str(config["base_url"]).rstrip("/"),
        default_headers=headers,
        max_retries=8,
    )
    kwargs: dict[str, Any] = {
        "model": str(config["model"]),
        "input": [{"role": "user", "content": str(config["prompt"])}],
        "instructions": str(config.get("system_prompt", "")).strip()
        or "You are a helpful assistant.",
        "store": False,
        "stream": True,
    }
    reasoning_effort = str(config.get("reasoning_effort", "")).strip()
    if reasoning_effort:
        kwargs["reasoning"] = {"effort": reasoning_effort}
    raw: dict[str, Any] = {}
    stream_events: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for event in client.responses.create(**kwargs):
        event_payload = event.model_dump() if hasattr(event, "model_dump") else {}
        if event_payload:
            stream_events.append(event_payload)
        event_type = str(event_payload.get("type", ""))
        if event_type == "response.output_text.delta":
            delta = event_payload.get("delta")
            if isinstance(delta, str):
                text_parts.append(delta)
        elif event_type in {
            "response.completed",
            "response.failed",
            "response.incomplete",
        }:
            response = event_payload.get("response")
            if isinstance(response, dict):
                raw = response
    if stream_events:
        raw = {**raw, "_stream_events": stream_events}
    return {
        "response": "".join(text_parts).strip() or _extract_response_text(raw),
        "response_id": raw.get("id", ""),
        "usage": raw.get("usage") if isinstance(raw.get("usage"), dict) else {},
        "raw_response": raw,
    }


def _run_gemini(config: dict[str, Any], api_key: str) -> dict[str, Any]:
    base_url = str(config["base_url"]).rstrip("/")
    model = quote(str(config["model"]), safe="")
    payload: dict[str, Any] = {
        "contents": [
            {"role": "user", "parts": [{"text": str(config["prompt"])}]},
        ],
    }
    system_prompt = str(config.get("system_prompt", "")).strip()
    if system_prompt:
        payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}
    raw = _post_json(
        f"{base_url}/models/{model}:generateContent",
        {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        payload,
    )
    parts: list[str] = []
    candidates = raw.get("candidates")
    if isinstance(candidates, list) and candidates:
        content = candidates[0].get("content") if isinstance(candidates[0], dict) else {}
        if isinstance(content, dict):
            for part in content.get("parts", []):
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
    return {
        "response": "\n".join(parts).strip(),
        "response_id": raw.get("responseId", ""),
        "usage": raw.get("usageMetadata")
        if isinstance(raw.get("usageMetadata"), dict)
        else {},
        "raw_response": raw,
    }


def _run_anthropic(config: dict[str, Any], api_key: str) -> dict[str, Any]:
    base_url = str(config["base_url"]).rstrip("/")
    payload: dict[str, Any] = {
        "model": str(config["model"]),
        "messages": [{"role": "user", "content": str(config["prompt"])}],
        "max_tokens": int(config.get("max_tokens") or 64_000),
    }
    system_prompt = str(config.get("system_prompt", "")).strip()
    if system_prompt:
        payload["system"] = system_prompt
    raw = _post_json(
        f"{base_url}/messages",
        {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        payload,
    )
    parts: list[str] = []
    content = raw.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return {
        "response": "\n".join(parts).strip(),
        "response_id": raw.get("id", ""),
        "usage": raw.get("usage") if isinstance(raw.get("usage"), dict) else {},
        "raw_response": raw,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    kind = str(config["kind"])
    api_key = os.environ["CAD_BENCH_API_KEY"]
    if kind == "openai_compatible":
        payload = _run_openai_compatible(config, api_key)
    elif kind == "openai_codex_responses":
        payload = _run_openai_codex_responses(config, api_key)
    elif kind == "gemini":
        payload = _run_gemini(config, api_key)
    elif kind == "anthropic":
        payload = _run_anthropic(config, api_key)
    else:
        raise ValueError(f"unsupported API model kind: {kind}")
    print(json.dumps(payload))


if __name__ == "__main__":
    main()
