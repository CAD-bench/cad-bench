import argparse
import json
import os
import sys
from pathlib import Path
from time import monotonic, sleep

from openai import OpenAI

DEFAULT_TIMEOUT_SECONDS = 72 * 60 * 60


def extract_text(raw_response):
    parts = []
    for item in raw_response.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") == "output_text" and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "\n".join(part for part in parts if part).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    prompt = str(config["prompt"])
    instructions = str(config.get("system_prompt", "")).strip()
    timeout_seconds = int(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    client = OpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        max_retries=8,
    )
    status_path = Path(args.config).with_name(".openai_response_status.jsonl")
    started_at = monotonic()

    def write_status(response) -> None:
        payload = {
            "response_id": getattr(response, "id", ""),
            "status": getattr(response, "status", ""),
            "timeout_seconds": timeout_seconds,
            "elapsed_seconds": round(monotonic() - started_at, 3),
        }
        with status_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")

    kwargs = {
        "model": str(config["model"]),
        "input": prompt,
        "background": True,
    }
    if instructions:
        kwargs["instructions"] = instructions
    reasoning_effort = str(config.get("reasoning_effort", "")).strip()
    if reasoning_effort:
        kwargs["reasoning"] = {"effort": reasoning_effort}
    access_mode = str(config.get("access_mode", "")).strip()
    tools = []
    if access_mode in {"web", "web_ci"}:
        tools.append({"type": "web_search"})
    if access_mode == "web_ci":
        tools.append({"type": "code_interpreter", "container": {"type": "auto"}})
    if tools:
        kwargs["tools"] = tools
    response = client.responses.create(**kwargs)
    write_status(response)
    while response.status in {"queued", "in_progress"}:
        if monotonic() - started_at >= timeout_seconds:
            detail = {
                "response_id": getattr(response, "id", ""),
                "status": getattr(response, "status", ""),
                "timeout_seconds": timeout_seconds,
            }
            print(json.dumps(detail), file=sys.stderr)
            raise SystemExit(124)
        sleep(10)
        response = client.responses.retrieve(response.id)
        write_status(response)
    raw_response = response.model_dump()
    status = str(raw_response.get("status", "")).strip()
    if status != "completed":
        detail = {
            "response_id": raw_response.get("id", ""),
            "status": status,
            "error": raw_response.get("error"),
            "incomplete_details": raw_response.get("incomplete_details"),
        }
        print(json.dumps(detail), file=sys.stderr)
        raise SystemExit(1)
    usage = response.usage.model_dump() if response.usage is not None else None
    payload = {
        "response": extract_text(raw_response),
        "response_id": raw_response.get("id", ""),
        "usage": usage,
        "raw_response": raw_response,
        "status_history_path": status_path.name,
    }
    print(json.dumps(payload))


if __name__ == "__main__":
    main()
