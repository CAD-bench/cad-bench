# CAD-bench dataset card

## Summary

CAD-bench measures whether an agent can produce executable parametric CAD that satisfies dimensional, topological, pose, assembly, and mechanical-function requirements. The public Harbor dataset contains 17 tasks ranging from single solids to gears and functional transmissions. A separate private dataset contains seven held-out functional transmission tasks.

| Dataset | Directory | Tasks | Harbor name |
|---|---|---:|---|
| Public benchmark | `dataset/` | 17 | `cad-bench/cad-bench` |

- Harbor task schema: 1.3
- Required submission: `/workspace/final.py`
- Build language: Python 3.11 with Build123D 0.10.0
- Required exported object: top-level `part`
- Primary task score: `overall_score` in `[0, 1]`

The dataset has not been published to the Harbor registry from this working copy. Local evaluation uses `harbor run --path dataset`.

## What an agent receives

The agent receives the task instruction and an isolated environment with Python, Build123D, and ordinary command-line development tools. Public network access is enabled during the agent phase so Harbor can install supported agents and so web-enabled evaluation policies remain possible.

The agent does not receive the verifier image, reference program, expected-value metadata, Blender simulation scripts, or grading code.

## Submission format

The submission is a Python file at `/workspace/final.py`. Running the file must construct the completed CAD result and assign it to `part`. The result may be a single Build123D shape or a compound/assembly appropriate to the task.

## Verification

Harbor runs the verifier in a separate no-network container. The submitted `final.py` crosses from the agent environment into that container. The fixture-backed fastener oracle additionally declares its STEP support asset so that Harbor rematerializes it at the same `/workspace/fixtures/` path; ordinary submissions remain a single file. Depending on the task, the verifier evaluates:

- successful Python execution and Build123D object construction
- dimensions, volume, surface area, bounding boxes, and pose
- hole, bore, slot, flange, fastener, and profile structure
- part count and part-aware classification
- collision and disjoint-body requirements
- gear placement and transmission ratios
- Blender rigid-body contact behavior for functional assemblies

The verifier writes a scalar `reward`, `overall_score`, `task_score`, `build_success`, a full `grading.json`, and task-specific scoring evidence. Task metadata selects a shared versioned verifier image; task-specific contracts and oracle material remain in the task directory.

## Aggregation

The public `dataset/metric.py` computes a mean within each difficulty tier, then combines represented tier means using:

| Difficulty | Tier weight |
|---|---:|
| Easy | 1 |
| Medium | 2 |
| Hard | 3 |
| Insane | 4 |

Missing trials count as zero and task scores are clamped to `[0, 1]`. Tier balancing is intentional: the benchmark score is not a plain task-count-weighted mean.

The private set uses the same aggregation code. Its functional reference programs are feasibility controls and need not receive a perfect contact-simulation score; validation requires that they build, satisfy the structural gates, and clear the pack's calibrated reference threshold.

## Intended use

CAD-bench is intended for:

- comparing tool-using agents and models on executable mechanical CAD
- evaluating prompting, reasoning effort, and web-access policies under the same task revision
- analyzing failures with Harbor trajectories, submitted source, meshes, and verifier evidence
- producing agent rollouts for subsequent improvement

Comparisons should report the exact dataset digest or registry version, agent, model, Harbor version, number of attempts, network policy, and aggregate metric.

## Private-set handling

The private functional refresh exists to reduce tuning against the public benchmark. It must remain access-controlled and is not present in this repository. Do not add it or derivatives of its task content to public source archives, public Harbor publications, public build contexts, or public CI artifacts.

## Limitations

- Automated geometric and rigid-body checks are engineering proxies, not manufacturing certification.
- Contact simulations are sensitive to meshing and numerical calibration; the exact verifier image reference is part of the result definition.
- Build123D execution establishes constructability in the benchmark environment, not printability, machinability, material selection, tolerance-stack correctness, or safety.
- Public tasks and their reference solutions may be available to model developers; use the private set for held-out confirmation.
- The agent phase permits public networking by default. A no-web comparison must explicitly change and report that policy.
