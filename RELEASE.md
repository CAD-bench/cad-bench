# CAD-bench Harbor release checks

`dataset/` is the complete public native Harbor dataset. Releases are content-addressed by the task and metric digests in `dataset/dataset.toml`.

## Local validation

Use Harbor 0.19.0 and verify that syncing does not change the manifest:

```bash
uvx --from 'harbor==0.19.0' harbor sync dataset
git diff --exit-code -- dataset/dataset.toml
```

Resolve and run a representative oracle task:

```bash
uvx --from 'harbor==0.19.0' harbor run \
  --path dataset/cube_20mm_z_minus \
  --agent oracle \
  --print-config

uvx --from 'harbor==0.19.0' harbor run \
  --path dataset/cube_20mm_z_minus \
  --agent oracle \
  --yes
```

## Public release boundary

This repository and its Harbor publication contain `dataset/` only. They must not contain the private functional refresh or derivatives of its instructions, reference programs, verifier assets, or task digests.

## Registry publication

Registry publication is a separate, explicit release action. Confirm the authenticated Harbor organization and the manifest diff immediately before publishing:

```bash
harbor auth login
harbor auth status
harbor sync dataset
harbor publish dataset --public --tag '<version>'
```

After publication, record the exact registry version and task digests used for every reported benchmark run.
