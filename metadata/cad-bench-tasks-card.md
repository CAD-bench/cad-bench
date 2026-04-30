---
license: other
license_name: cad-bench-mixed-terms
license_link: https://www.mcmaster.com/termsandconditions
pretty_name: CAD-bench Full Reviewer Artifact
language:
- en
tags:
- cad
- benchmark
- build123d
- step
- mlcroissant
- language-model-evaluation
size_categories:
- n<1K
---

# CAD-bench Full Reviewer Artifact

This dataset contains the full anonymous CAD-bench reviewer artifact. It
includes the public task payloads, reported result JSON, source archive, and
provenance report artifacts for the runs used by the paper tables.

Each task directory includes:

- `prompt.txt`: the natural-language benchmark prompt
- `task.toml`: task metadata, difficulty, evaluator name, and expected values
- `gold.py`: a reference Build123D solution used for validation and media generation
- optional fixtures such as STEP files or Blender simulation scripts

The `results/cad-bench-reported-results.json` artifact contains every complete
17-task row from the CAD-bench website payload used by the paper tables. The
JSON separates 19 standalone model rows from 32 agent rows. The companion
`results/cad-bench-task-aggregates.json` artifact contains the per-task means
reported in the paper, computed from the 867 task rows in the provenance
reports.

The `provenance/` directory mirrors the approved run manifest and downloaded
report artifacts for the published rows. The `code/` directory contains the
anonymous source archive for the benchmark runtime and scoring code.

For task-only runtime loading, use the companion task dataset:

```bash
HF_TASKS_REPO_ID=CAD-bench/cad-bench-ed-2026-anonymous-tasks
```

## Intended Use

Use this dataset with the CAD-bench runtime to evaluate CAD code-generation or
agentic CAD systems. Scores are diagnostic benchmark signals; they are not
certifications that generated mechanical parts are safe to manufacture or deploy.

## Data Provenance

The tasks are synthetic CAD prompts and benchmark metadata authored for this
benchmark. They do not contain personal data or human-subject records. The M3
socket-head task includes a STEP fixture for a standard commercial fastener,
documented in the task metadata and paper license table.

## Licensing

Authored benchmark code, prompts, task metadata, and reference programs are
released under MIT. The M3 socket-head screw fixture is a McMaster-Carr CAD
download for part `91290A111`; use of that fixture is governed by
McMaster-Carr's website and CAD download terms.

## Limitations

The release has 17 tasks. Some simple geometry tasks are close to solved by
current models, while functional assembly tasks remain difficult. The current
reference implementations use Build123D, although the benchmark is intended to
score submitted CAD artifacts rather than a particular modeling API.

## Citation

Anonymous Author(s). CAD-bench: An Executable Benchmark for Language-Model CAD
Agents. NeurIPS Evaluations & Datasets submission, 2026.
