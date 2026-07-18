from __future__ import annotations

import json
import os
from pathlib import Path

from tasks.specs import load_task_spec


TASK_ID = os.environ["CAD_BENCH_TASK_ID"]
SUBMISSION = Path("/workspace/final.py")
VERIFIER_LOGS = Path("/logs/verifier")


def main() -> None:
    VERIFIER_LOGS.mkdir(parents=True, exist_ok=True)
    code = SUBMISSION.read_text(encoding="utf-8") if SUBMISSION.is_file() else ""
    evaluation = load_task_spec(TASK_ID).evaluate_code(
        code,
        provenance_dir=VERIFIER_LOGS / "scoring",
    )
    result = {"task_id": TASK_ID, "raw": evaluation.raw, "metrics": evaluation.metrics}
    (VERIFIER_LOGS / "grading.json").write_text(
        json.dumps(result, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    numeric = ("reward", "overall_score", "task_score", "build_success")
    rewards = {
        key: float(evaluation.metrics[key])
        for key in numeric
        if isinstance(evaluation.metrics.get(key), (int, float))
        and not isinstance(evaluation.metrics.get(key), bool)
    }
    (VERIFIER_LOGS / "reward.json").write_text(
        json.dumps(rewards, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
