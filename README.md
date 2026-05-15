# CAD-bench

CAD-bench is a CAD benchmark for LLMs and agentic harnesses. It currently has 17 tasks: 3 easy, 4 medium, 6 hard, and 4 insane.

The repo is set up for two use cases:

- reproduce the published harness runs
- evaluate your own harness against the same task set and scoring code
- read the public paper PDF and inspect reported-result metadata

Public task payloads are intended to live on Hugging Face. This repo keeps the benchmark runtime, scoring code, and harnesses; the published static website lives in the separate `CAD-bench/cad-bench.github.io` repo.

The public paper PDF is available at `paper/main.pdf`. LaTeX sources are
intentionally not included in this public repo.

## Scoring & Tasks

Each task provides:
- `task.toml`: metadata, difficulty, evaluator, expected values
- `prompt.txt`: benchmark prompt
- `gold.py`: reference Build123D solution
- optional fixtures or Blender assets

Task files are loaded either from a local `tasks/<task_id>/` tree or from the HF dataset configured by `HF_TASKS_REPO_ID`. If `HF_TASKS_REPO_ID` is unset, the loader derives a default public task repo from the owner of `HF_PROVENANCE_REPO_ID`. The anonymous NeurIPS 2026 task-only reviewer mirror uses `CAD-bench/cad-bench-ed-2026-anonymous-tasks` at the revision recorded in `RELEASE.md`.

Per-task scoring returns:
- `submission_exists`
- `build_success`
- `task_score`
- `overall_score`
- `reward = 0.05 * build_success + 0.95 * overall_score`

The benchmark-level aggregate is the difficulty-weighted mean of `overall_score`, with weights `easy=1`, `medium=2`, `hard=3`, `insane=4`.

## Requirements

- Python 3.11
- `uv`
- Docker
- Blender on `PATH` for the Blender-scored gearbox tasks and `export-task-media`

## Setup

```bash
uv sync
cp .env.example .env
```

Fill all mandatory variables and optional variables depending on which harnesses you want to evaluate into .env.

```bash
docker build -t cad-build123d-bench .
```

`eval-harness` uses these variables:
- always: `CAD_BENCH_AGENT_IMAGE`, `HF_PROVENANCE_REPO_ID`
- optional: `HF_TASKS_REPO_ID` and `HF_TASKS_REVISION` to pin the public task dataset
- `HF_TOKEN` for uploading provenance to Hugging Face (the provenance takes up a lot of disk space, so it is currently only supported on HuggingFace)
- OpenAI harnesses: `OPENAI_API_KEY`
- Codex and Pi harnesses: `CODEX_AUTH_JSON_B64` (JSON code authentication token located in $HOME/.codex; API-only runs will likely be prohibitively expensive)
- Gemini harnesses: `GEMINI_API_KEY` or `GOOGLE_API_KEY`
- Vercel AI Gateway harnesses: `AI_GATEWAY_API_KEY`
- `EXA_API_KEY`: optional for more consistent Pi web search


## Reproducing Built-in Harnesses

Run one harness across all 17 tasks:

```bash
uv run eval-harness \
  --harness harnesses/codex/harnesses.py:gpt_5_4_web_low
```

Run one harness on a subset:

```bash
uv run eval-harness \
  --harness harnesses/openai/harnesses.py:gpt_5_4_mini_offline_med \
  --task-ids cube_20mm_z_minus,box_30x20x10_z_plus
```

Run the built-in matrix once:

```bash
uv run python scripts/run_clean_matrix.py
```

Useful filters:

```bash
uv run python scripts/run_clean_matrix.py \
  --providers openai \
  --models gpt-5.4-mini \
  --accesses web,offline
```

Built-in harness refs always use:

```text
harnesses/<provider>/harnesses.py:<function_name>
```

Examples:

- `harnesses/codex/harnesses.py:gpt_5_4_web_low`
- `harnesses/pi/harnesses.py:gpt_5_4_mini_offline_high`
- `harnesses/openai/harnesses.py:gpt_5_4_nano_web_ci_med`

## Evaluate Your Own Harness

The loader expects a harness ref in the same form:

```text
path/to/harnesses.py:symbol_name
```

That module must provide:

- an exported `symbol_name` whose value, or return value, is a `HarnessSpec`
- `build_prompt(spec, task_prompt) -> tuple[str | None, str]`
- `run(runtime, spec, system_prompt, prompt, workdir, submission_dir, image, ...)`

Custom harnesses are not restricted to the built-in provider, strategy, or access names. The simplest path is still to copy one of the built-in harness modules under `harnesses/` and change only the provider-specific code.

Two harness styles already exist:

- `one_shot_code`: return Build123D code wrapped in `<build123d_code>...</build123d_code>`
- `agent_step`: write the final STEP file to `~/final.step` inside the guest container

## Published Results Site

Published website results come from `approved_runs.json` inside the
`CAD-bench/cad-bench.github.io` repo, not directly from `logs/` or blind scans of the provenance repo.
Do not mutate that repo from benchmark runners. Review website entries in the website repo,
then render the static HTML from that checked-out tree.

Render the site into a checked-out `cad-bench.github.io` working tree:

```bash
uv run python scripts/render_site.py \
  --approved-runs-json /path/to/cad-bench.github.io/approved_runs.json \
  --output-html /path/to/cad-bench.github.io/index.html
```

Upload public task definitions from a local task mirror to Hugging Face:

```bash
uv run upload-public-tasks --tasks-root /path/to/cad-bench-tasks/tasks
```

Export hash canaries for private or unreleased tasks:

```bash
uv run export-task-canaries --tasks-root /path/to/private-tasks --out outputs/private_task_canaries.json --salt-env PRIVATE_TASK_CANARY_SALT
```

## Useful Commands

Generate/update the offline Build123D docs bundle:

```bash
uv run build123d-docs-bundle
```

Render fixed views from candidate code (for manual inspection of agent results):

```bash
uv run render-cad-views /path/to/candidate.py --out ./renders --prefix sample
```

Export reference images and videos:

```bash
uv run export-task-media --out outputs/cad_benchmark_media --skip-send
```

Run tests:

```bash
uv run pytest
```

## Release Metadata

NeurIPS release notes live in `RELEASE.md`. The full reviewer artifact
Croissant metadata is `metadata/cad-bench-tasks-croissant.json`; the Hugging
Face dataset cards are `metadata/cad-bench-tasks-card.md` for the full artifact
and `metadata/cad-bench-tasks-only-card.md` for the task-only runtime dataset.
