# CAD-bench

CAD-bench is a native [Harbor](https://www.harborframework.com/) benchmark for evaluating agents that construct mechanical parts and functional assemblies in Build123D. The repository contains the tasks and domain verifier; Harbor supplies agent adapters, model configuration, sandbox lifecycle, retries, concurrency, trajectories, artifacts, result storage, and result inspection.

The active public benchmark is [`dataset/`](dataset), with 17 self-contained tasks. A separate access-controlled seven-task functional refresh exists for held-out evaluation but is intentionally absent from this public repository. There is no benchmark-specific runner, model harness, container orchestrator, provenance uploader, or remote task loader.

## Install Harbor

The datasets are validated against Harbor 0.19.0:

```bash
uv tool install 'harbor==0.19.0'
```

Docker must be available for local runs. Until the versioned images are published, build the two shared images once:

```bash
./images/build.sh
```

Every task then runs independently with ordinary Harbor using the image references in its own `task.toml`.

## Run CAD-bench

Run the complete public dataset with any Harbor-supported agent and model:

```bash
harbor run \
  --path dataset \
  --agent '<agent>' \
  --model '<model>' \
  --n-concurrent 4
```

Run one task while developing:

```bash
harbor run \
  --path dataset/cube_20mm_z_minus \
  --agent '<agent>' \
  --model '<model>'
```

Run an oracle smoke test without configuring a model:

```bash
harbor run --path dataset/cube_20mm_z_minus --agent oracle
```

Harbor writes jobs to `jobs/` by default. Each trial includes its resolved configuration, trajectory, verifier output, declared `final.py` artifact, and rewards. Inspect them with:

```bash
harbor view jobs
```

## Task contract

Each task asks the agent to create:

```text
/workspace/final.py
```

The file must run with Python 3.11 and Build123D 0.10.0 and leave the completed model in a top-level variable named `part`.

Grading runs in Harbor's `separate` verifier mode with networking disabled. Harbor stops the agent environment, transfers the declared `/workspace/final.py` artifact, and starts the shared verifier image named in `task.toml`. The fixture-backed fastener oracle also transfers its STEP support asset at the declared `/workspace/fixtures/` path; ordinary submissions remain a single `final.py` file. The verifier executes the candidate, checks the resulting topology and geometry, and applies the task-specific deterministic or Blender rigid-body evaluation. It writes:

- `reward.json`: Harbor reward channels (`reward`, `overall_score`, `task_score`, and `build_success`)
- `grading.json`: the complete scoring record
- `scoring/`: meshes, simulation records, renders, and failure evidence produced during grading

The verifier image contains the shared evaluator plus public task contracts, reference programs, and functional simulation assets. None is mounted into the agent environment.

## Dataset layout

```text
dataset/
├── dataset.toml                 # Harbor dataset manifest and exact task digests
├── metric.py                    # CAD-bench aggregate metric
└── <task_id>/
    ├── instruction.md
    ├── task.toml                # Harbor schema, image refs, task selector
    ├── environment/             # required Harbor directory; image is prebuilt
    ├── contract.toml            # task-specific scoring values
    └── solution/                # Harbor oracle and reference program

images/                          # one agent image and one verifier image
verifier/                        # shared evaluator and three simulation families
```

`dataset/` owns task-specific data, `verifier/` owns shared scoring logic, and `images/` owns container dependencies. Rebuild the images and refresh the manifest after changing any of them:

```bash
harbor sync dataset
git diff -- dataset/dataset.toml
```

The public custom metric first computes the mean `overall_score` within each represented difficulty tier, then combines tier means with weights 1 for easy, 2 for medium, 3 for hard, and 4 for insane. Missing rewards count as zero. This preserves the benchmark's tier-balanced score rather than allowing a tier with more tasks to dominate.

## Private dataset policy

The private development dataset contains exactly seven unreleased functional tasks:

- two direct spur-gear transfers
- three three-shaft idler transfers
- two compound right-angle transfers

It stays in the private development repository and must not be copied here or published publicly. Its Harbor manifest name is `cad-bench/cad-bench-functional-v1` so it can later be published privately and access-controlled through the Harbor registry.

## Publishing later

Publishing is intentionally separate from development. When a public release is approved:

```bash
harbor auth login
harbor auth status
harbor sync dataset
harbor publish dataset --public --tag '<version>'
```

No registry release is implied by this working copy. Until one exists, run with `--path`.

## Research artifacts

The paper PDF and JSON files under `metadata/` are retained research records. They are not executable benchmark infrastructure. New evaluations should use Harbor job results as the canonical run record.

See [`DATA_CARD.md`](DATA_CARD.md) for scope, grading, aggregation, and limitations, and [`RELEASE.md`](RELEASE.md) for the local release checks.
