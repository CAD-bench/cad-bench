from __future__ import annotations

import base64
import json
import statistics
from functools import lru_cache
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
ENV_KEY_ORDER = (
    "CAD_BENCH_AGENT_IMAGE",
    "OPENAI_API_KEY",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "EXA_API_KEY",
    "HF_TOKEN",
    "HF_PROVENANCE_REPO_ID",
    "HF_TASKS_REPO_ID",
    "HF_TASKS_REVISION",
    "CODEX_AUTH_JSON_B64",
)
BUILD_SUCCESS_REWARD_WEIGHT = 0.05
OVERALL_SCORE_REWARD_WEIGHT = 1.0 - BUILD_SUCCESS_REWARD_WEIGHT
DIFFICULTY_WEIGHTS = {
    "easy": 1.0,
    "medium": 2.0,
    "hard": 3.0,
    "insane": 4.0,
}


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _resolve_env_path(root: Path | None = None) -> Path:
    if root is None:
        return ENV_PATH
    candidate = Path(root)
    return candidate if candidate.name == ".env" else candidate / ".env"


def _render_env_value(value: str) -> str:
    if "\n" in value:
        raise ValueError("multi-line .env values are not supported")
    return value


def _render_env_file(values: dict[str, str]) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for key in ENV_KEY_ORDER:
        if key in values:
            lines.append(f"{key}={_render_env_value(str(values[key]))}")
            seen.add(key)
    for key in sorted(values):
        if key in seen:
            continue
        lines.append(f"{key}={_render_env_value(str(values[key]))}")
    return "\n".join(lines) + "\n"


@lru_cache(maxsize=1)
def load_repo_env() -> dict[str, str]:
    return _parse_env_file(ENV_PATH)


def reload_repo_env() -> dict[str, str]:
    load_repo_env.cache_clear()
    return load_repo_env()


def load_env_file(root: Path | None = None) -> dict[str, str]:
    path = _resolve_env_path(root)
    if path == ENV_PATH:
        return load_repo_env()
    return _parse_env_file(path)


def env_value(name: str, default: str = "", root: Path | None = None) -> str:
    return str(load_env_file(root).get(name, default)).strip()


def required_env_value(name: str, root: Path | None = None) -> str:
    value = env_value(name, root=root)
    if not value:
        raise RuntimeError(f"{name} is required in {_resolve_env_path(root)}")
    return value


def update_repo_env(updates: dict[str, str]) -> dict[str, str]:
    values = dict(load_repo_env())
    for key, value in updates.items():
        values[str(key)] = str(value)
    ENV_PATH.write_text(_render_env_file(values), encoding="utf-8")
    return reload_repo_env()


def env_json_value(name: str) -> dict[str, Any]:
    raw = required_env_value(name)
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise TypeError(f"{name} must decode to a JSON object")
    return payload


def env_b64_json_value(name: str) -> dict[str, Any]:
    raw = required_env_value(name)
    decoded = base64.b64decode(raw.encode("ascii")).decode("utf-8")
    payload = json.loads(decoded)
    if not isinstance(payload, dict):
        raise TypeError(f"{name} must decode to a JSON object")
    return payload


def set_env_b64_json_value(name: str, payload: dict[str, Any]) -> dict[str, str]:
    encoded = base64.b64encode(
        (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    ).decode("ascii")
    return update_repo_env({name: encoded})


def clamp_unit_interval(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def summarize_difficulty_aggregates(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, float], dict[str, int], float]:
    by_difficulty: dict[str, float] = {}
    counts: dict[str, int] = {}
    weighted_total = 0.0
    total_weight = 0.0
    for difficulty, weight in DIFFICULTY_WEIGHTS.items():
        scores = [
            float(row["metrics"]["overall_score"])
            for row in rows
            if str(row.get("difficulty")) == difficulty
        ]
        if not scores:
            continue
        aggregate = clamp_unit_interval(statistics.mean(scores))
        count = len(scores)
        by_difficulty[difficulty] = aggregate
        counts[difficulty] = count
        weighted_total += aggregate * float(weight)
        total_weight += float(weight)
    benchmark_score = weighted_total / total_weight if total_weight else 0.0
    return by_difficulty, counts, benchmark_score
