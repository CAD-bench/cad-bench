from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench import runner as bench


def builtin_harness(provider: str, model: str, access: str, level: str) -> str:
    return bench.builtin_harness_ref(provider, model, access, level)


def load_builtin_harness(provider: str, model: str, access: str, level: str):
    return bench.load_harness_spec(builtin_harness(provider, model, access, level))
