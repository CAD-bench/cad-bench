from __future__ import annotations

from typing import Any

from harnesses.api_models import utils as api_utils
from harnesses.utils import register_builtin_harnesses

PROVIDER = "vercel"
SUPPORTED_CONFIGS = (
    ("alibaba/qwen3.6-27b", "offline", "none"),
    ("alibaba/qwen-3.6-max-preview", "offline", "none"),
)


def make_harness(model: str, mode: str, level: str):
    return api_utils.make_harness(PROVIDER, model, mode, level)


def build_prompt(spec: Any, task_prompt: str) -> tuple[str | None, str]:
    return api_utils.build_prompt(spec, task_prompt)


def run(**kwargs):
    return api_utils.run(provider=PROVIDER, **kwargs)


register_builtin_harnesses(
    globals(),
    configs=SUPPORTED_CONFIGS,
    make_harness=make_harness,
)
