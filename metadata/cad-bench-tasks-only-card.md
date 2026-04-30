---
license: other
license_name: cad-bench-mixed-terms
license_link: https://www.mcmaster.com/termsandconditions
pretty_name: CAD-bench Task Payloads
language:
- en
tags:
- cad
- benchmark
- build123d
- step
- language-model-evaluation
size_categories:
- n<1K
---

# CAD-bench Task Payloads

This dataset contains only the public task payloads for CAD-bench, an
execution-based benchmark for language-model CAD agents. It is the lightweight
runtime dataset used by the benchmark loader.

Each task directory includes:

- `prompt.txt`: the natural-language benchmark prompt
- `task.toml`: task metadata, difficulty, evaluator name, and expected values
- `gold.py`: a reference Build123D solution used for validation and media generation
- optional fixtures such as STEP files or Blender simulation scripts

It also includes `tasks_manifest.json`, which records per-task bundle hashes.
This dataset intentionally does not include benchmark result rows, source
archives, or run provenance. Those are in the companion full reviewer artifact:
`CAD-bench/cad-bench-ed-2026-anonymous-full`.

## Runtime Use

```bash
HF_TASKS_REPO_ID=CAD-bench/cad-bench-ed-2026-anonymous-tasks
```

Pin `HF_TASKS_REVISION` to the revision recorded in the release notes when
reproducing the paper exactly.

## Licensing

Authored benchmark code, prompts, task metadata, and reference programs are
released under MIT. The M3 socket-head screw fixture is a McMaster-Carr CAD
download for part `91290A111`; use of that fixture is governed by
McMaster-Carr's website and CAD download terms.
