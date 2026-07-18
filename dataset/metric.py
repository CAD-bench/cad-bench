# /// script
# dependencies = []
# ///
from __future__ import annotations

import argparse
import json
from pathlib import Path


DIFFICULTIES = ['easy', 'easy', 'easy', 'medium', 'medium', 'medium', 'medium', 'hard', 'hard', 'hard', 'hard', 'hard', 'hard', 'insane', 'insane', 'insane', 'insane']
WEIGHTS = {'easy': 1.0, 'medium': 2.0, 'hard': 3.0, 'insane': 4.0}


def main(input_path: Path, output_path: Path) -> None:
    rewards = [json.loads(line) for line in input_path.read_text(encoding="utf-8").splitlines()]
    if len(rewards) != len(DIFFICULTIES):
        raise ValueError(f"expected {len(DIFFICULTIES)} task rewards, got {len(rewards)}")
    grouped: dict[str, list[float]] = {difficulty: [] for difficulty in WEIGHTS}
    for difficulty, reward in zip(DIFFICULTIES, rewards, strict=True):
        score = 0.0 if reward is None else float(reward.get("overall_score", reward.get("reward", 0.0)))
        grouped[difficulty].append(max(0.0, min(1.0, score)))
    tier_means = {
        difficulty: sum(values) / len(values)
        for difficulty, values in grouped.items()
        if values
    }
    total_weight = sum(WEIGHTS[difficulty] for difficulty in tier_means)
    benchmark_score = (
        sum(tier_means[difficulty] * WEIGHTS[difficulty] for difficulty in tier_means)
        / total_weight
        if total_weight
        else 0.0
    )
    output = {"benchmark_score": benchmark_score}
    output.update({f"{difficulty}_score": score for difficulty, score in tier_means.items()})
    output_path.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--input-path", type=Path, required=True)
    parser.add_argument("-o", "--output-path", type=Path, required=True)
    args = parser.parse_args()
    main(args.input_path, args.output_path)
