from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass
from pathlib import Path

from bench.config import env_value
from bench import runner as bench
from harnesses.api_models.utils import api_key_env_names, env_value_with_process_fallback
from harnesses import utils as harness_utils

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER = REPO_ROOT / ".venv" / "bin" / "eval-harness"


@dataclass(frozen=True)
class HarnessRef:
    provider: str
    model: str
    access: str
    level: str

    @property
    def harness_id(self) -> str:
        return f"{self.provider}/{self.model}-{self.access}-{self.level}"

    @property
    def ref(self) -> str:
        return str(
            bench.builtin_harness_ref(
                self.provider, self.model, self.access, self.level
            )
        )


def all_harness_refs() -> list[HarnessRef]:
    return [
        HarnessRef(provider=provider, model=model, access=access, level=level)
        for provider, model, access, level in harness_utils.builtin_harness_matrix()
    ]


def run_harness_tasks(harness: HarnessRef, task_ids: list[str]) -> int:
    cmd = [
        str(RUNNER),
        "--harness",
        harness.ref,
        "--task-ids",
        ",".join(task_ids),
    ]
    print(f"RUN {harness.harness_id} tasks={len(task_ids)}")
    proc = subprocess.run(cmd, cwd=REPO_ROOT, check=False)
    print(f"DONE {harness.harness_id} rc={proc.returncode}")
    return int(proc.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the selected built-in harness matrix once."
    )
    parser.add_argument(
        "--providers",
        default="codex,pi,openai",
        help="Comma-separated provider filter. Use a subset such as openai or kimi,deepseek,gemini,claude.",
    )
    parser.add_argument(
        "--models",
        default="",
        help="Optional comma-separated model filter, e.g. gpt-5.4-mini,gpt-5.4.",
    )
    parser.add_argument(
        "--accesses",
        default="",
        help="Optional comma-separated access-mode filter, e.g. web,offline,web_ci.",
    )
    parser.add_argument(
        "--levels",
        default="",
        help="Optional comma-separated reasoning-level filter, e.g. low,med,high,xhigh.",
    )
    parser.add_argument(
        "--task-ids",
        default="",
        help="Optional comma-separated task ids. Empty runs the full benchmark.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the selected harnesses without running them.",
    )
    args = parser.parse_args()

    if not RUNNER.exists():
        raise FileNotFoundError(f"missing eval-harness runner at {RUNNER}")

    provider_filter = {
        item.strip() for item in args.providers.split(",") if item.strip()
    }
    if not provider_filter:
        raise ValueError("at least one provider must be selected")
    known_providers = set(harness_utils.BUILTIN_HARNESS_PROVIDERS)
    unknown_providers = provider_filter - known_providers
    if unknown_providers:
        raise ValueError(f"unknown providers: {sorted(unknown_providers)}")

    model_filter = {item.strip() for item in args.models.split(",") if item.strip()}
    access_filter = {
        item.strip() for item in args.accesses.split(",") if item.strip()
    }
    level_filter = {item.strip() for item in args.levels.split(",") if item.strip()}
    task_ids = [item.strip() for item in args.task_ids.split(",") if item.strip()]
    if task_ids:
        bench.select_task_specs(task_ids)
    else:
        task_ids = list(bench.TASK_IDS)

    harnesses = [
        harness
        for harness in all_harness_refs()
        if harness.provider in provider_filter
        and (not model_filter or harness.model in model_filter)
        and (not access_filter or harness.access in access_filter)
        and (not level_filter or harness.level in level_filter)
    ]
    if not harnesses:
        raise ValueError("no harnesses matched the requested provider/model filters")

    print(f"providers={','.join(sorted(provider_filter))}")
    if model_filter:
        print(f"models={','.join(sorted(model_filter))}")
    if access_filter:
        print(f"accesses={','.join(sorted(access_filter))}")
    if level_filter:
        print(f"levels={','.join(sorted(level_filter))}")
    print(f"total_harnesses={len(harnesses)} total_tasks={len(task_ids)}")

    for harness in harnesses:
        print(f"SELECTED {harness.harness_id}")
    if args.dry_run:
        return 0

    if "openai" in provider_filter and not env_value("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required in .env")
    api_key_providers = {
        provider
        for provider in provider_filter - {"codex", "pi", "openai", "openai_codex"}
        if api_key_env_names(provider)
    }
    for provider in sorted(api_key_providers):
        key_names = api_key_env_names(provider)
        if not any(env_value_with_process_fallback(name) for name in key_names):
            raise RuntimeError(
                f"{' or '.join(key_names)} is required in .env or the process environment"
            )
    if provider_filter & {"codex", "pi", "openai_codex"}:
        from harnesses.codex.utils import has_codex_auth_token

        if not has_codex_auth_token():
            raise RuntimeError("CODEX_AUTH_JSON_B64 is required in .env")

    failed = False
    for harness in harnesses:
        failed = run_harness_tasks(harness, task_ids) != 0 or failed
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
