from harnesses.openai.utils import build_prompt, make_harness, run  # noqa: F401
from harnesses.utils import register_builtin_harnesses

MODEL_RULES = {
    "gpt-5.5": {
        "access_modes": ("web", "offline", "web_ci"),
        "levels": ("low", "med", "high", "xhigh"),
    },
    "gpt-5.5-pro": {
        "access_modes": ("web", "offline"),
        "levels": ("med", "high", "xhigh"),
    },
    "gpt-5.5-mini": {
        "access_modes": ("web", "offline", "web_ci"),
        "levels": ("low", "med", "high", "xhigh"),
    },
    "gpt-5.5-nano": {
        "access_modes": ("web", "offline", "web_ci"),
        "levels": ("low", "med", "high", "xhigh"),
    },
    "gpt-5.4": {
        "access_modes": ("web", "offline", "web_ci"),
        "levels": ("low", "med", "high", "xhigh"),
    },
    "gpt-5.4-pro": {
        "access_modes": ("web", "offline"),
        "levels": ("med", "high", "xhigh"),
    },
    "gpt-5.4-mini": {
        "access_modes": ("web", "offline", "web_ci"),
        "levels": ("low", "med", "high", "xhigh"),
    },
    "gpt-5.4-nano": {
        "access_modes": ("web", "offline", "web_ci"),
        "levels": ("low", "med", "high", "xhigh"),
    },
}

SUPPORTED_CONFIGS = tuple(
    (model, access, level)
    for model, rule in MODEL_RULES.items()
    for access in rule["access_modes"]
    for level in rule["levels"]
)

register_builtin_harnesses(
    globals(),
    configs=SUPPORTED_CONFIGS,
    make_harness=make_harness,
)
