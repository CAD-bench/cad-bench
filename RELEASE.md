# CAD-bench Release Notes

This file is the release checklist for the NeurIPS 2026 Evaluations & Datasets
submission. It records the concrete artifacts needed to reproduce the paper
without relying on local machine state.

## Code

- Anonymous reviewer code artifact: `code/cad-bench-anonymous-source.tar.gz` in
  the reviewer dataset mirror and OpenReview supplemental material
- License: MIT, see `LICENSE`
- Runtime: Python 3.11 with `uv.lock`
- Main verification command: `uv run pytest`
- Lint command: `uv run ruff check .`

## Hugging Face Datasets

- Task-only reviewer dataset: https://huggingface.co/datasets/CAD-bench/cad-bench-ed-2026-anonymous-tasks
- Task-only paper revision: `fbcba766afcb4d4469d0297ceb819db2527c707d`
- Full reviewer artifact dataset: https://huggingface.co/datasets/CAD-bench/cad-bench-ed-2026-anonymous-full
- Runtime variables:
  - `HF_TASKS_REPO_ID=CAD-bench/cad-bench-ed-2026-anonymous-tasks`
  - `HF_TASKS_REVISION=fbcba766afcb4d4469d0297ceb819db2527c707d`
- Croissant metadata: `metadata/cad-bench-tasks-croissant.json`
- Full dataset card source: `metadata/cad-bench-tasks-card.md`
- Task-only dataset card source: `metadata/cad-bench-tasks-only-card.md`
- Reviewer result artifact: `results/cad-bench-reported-results.json` in the
  full reviewer artifact dataset, generated from `metadata/cad-bench-reported-results.json`
- Per-task aggregate artifact: `results/cad-bench-task-aggregates.json` in the
  full reviewer artifact dataset, generated from `metadata/cad-bench-task-aggregates.json`

The task-only dataset contains task prompts, TOML metadata, reference Build123D
programs, fixtures, and a `tasks_manifest.json` with per-task bundle hashes.
The full reviewer artifact dataset contains those same tasks plus reviewer
results, source archive, approved run manifest, and downloaded provenance
report artifacts.

## Result Artifacts

The paper reports every complete valid full 17-task row displayed by the
CAD-bench website payload and mirrored in the reviewer result artifact. The
current payload contains 19 standalone model rows and 32 agent rows. Each
reported row names its exact run directory in Appendix A. The local reports also contain
uploaded provenance URLs under
`storage.run_url` when the Hugging Face upload completed.
The per-task paper table is computed from those complete run reports:
51 full runs x 17 tasks = 867 task rows.

Provider infrastructure failures are retained in logs for accounting but
excluded from model-quality comparisons because they do not produce a valid
model evaluation. A report is leaderboard-eligible only when it covers all 17
tasks and each row reached model output plus scoring rather than failing at API
availability, provider quota, harness setup, or upload infrastructure.

## Submission Invariants

- Upload or refresh `metadata/cad-bench-tasks-croissant.json` with the full
  reviewer artifact dataset.
- Upload `metadata/cad-bench-tasks-card.md` as the full artifact dataset
  `README.md`.
- Upload `metadata/cad-bench-tasks-only-card.md` as the task-only dataset
  `README.md`.
- Keep the pinned task-only dataset revision fixed throughout review.
