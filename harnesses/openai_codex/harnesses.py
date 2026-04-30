from __future__ import annotations

from typing import Any

from harnesses.api_models import utils as api_utils
from harnesses.utils import REASONING_EFFORTS, register_builtin_harnesses

PROVIDER = "openai_codex"
MODELS = (
    "gpt-5.3-codex-spark",
    "gpt-5.5",
)
ACCESS_MODES = ("offline",)
LEVELS = ("low", "med", "high", "xhigh")

SUPPORTED_CONFIGS = tuple(
    (model, access, level)
    for model in MODELS
    for access in ACCESS_MODES
    for level in LEVELS
)


def make_harness(model: str, mode: str, level: str):
    spec = api_utils.make_harness(PROVIDER, model, mode, "none")
    return spec.__class__(
        harness_id=f"openai/{model}-{mode}-{level}",
        provider=spec.provider,
        strategy=spec.strategy,
        model=spec.model,
        reasoning_effort=REASONING_EFFORTS[level],
        access=spec.access,
        system_prompt=spec.system_prompt,
    )


def build_prompt(spec: Any, task_prompt: str) -> tuple[str | None, str]:
    return api_utils.build_prompt(spec, task_prompt)


def run(**kwargs):
    return api_utils.run(provider=PROVIDER, **kwargs)


register_builtin_harnesses(
    globals(),
    configs=SUPPORTED_CONFIGS,
    make_harness=make_harness,
)
