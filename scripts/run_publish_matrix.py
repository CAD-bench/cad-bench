from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from huggingface_hub import HfApi

from bench.config import env_value
from bench.provenance import resolve_hf_token
from bench.runner import TASK_IDS
from scripts.run_clean_matrix import HarnessRef, all_harness_refs

REPO_ROOT = Path(__file__).resolve().parent.parent
FULL_BENCHMARK_TASK_COUNT = len(TASK_IDS)
PROVIDER_PRIORITY = {
    "codex": 0,
    "pi": 1,
    "openai": 2,
}
MODEL_PRIORITY = {
    "gpt-5.4": 0,
    "gpt-5.4-mini": 1,
    "gpt-5.3-codex-spark": 2,
}
ACCESS_PRIORITY = {
    "web": 0,
    "offline": 1,
    "offline_nodocs": 2,
    "web_ci": 3,
}
LEVEL_PRIORITY = {
    "xhigh": 0,
    "high": 1,
    "med": 2,
    "low": 3,
}


@dataclass(frozen=True)
class ApprovedHFReport:
    repo_id: str
    revision: str
    path_in_repo: str

    def to_json(self) -> dict[str, str]:
        return {
            "kind": "hf_report",
            "repo_id": self.repo_id,
            "revision": self.revision,
            "path_in_repo": self.path_in_repo,
        }


@dataclass(frozen=True)
class HarnessOutcome:
    harness: HarnessRef
    run_dir: Path | None
    returncode: int
    worker_name: str
    run_error: str = ""


def _run(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    merged_env.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    merged_env.setdefault("UV_NO_PROGRESS", "1")
    if env:
        merged_env.update(env)
    return subprocess.run(cmd, cwd=cwd, env=merged_env, text=True, check=False)


def _run_checked(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    error: str,
) -> None:
    proc = _run(cmd, cwd=cwd, env=env)
    if proc.returncode != 0:
        raise RuntimeError(error)


def _capture(cmd: list[str], *, cwd: Path) -> str:
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _selected_harnesses(
    *, providers: set[str], models: set[str], accesses: set[str]
) -> list[HarnessRef]:
    harnesses = [
        harness
        for harness in all_harness_refs()
        if harness.provider in providers
        and (not models or harness.model in models)
        and (not accesses or harness.access in accesses)
    ]
    harnesses.sort(
        key=lambda harness: (
            PROVIDER_PRIORITY.get(harness.provider, 99),
            MODEL_PRIORITY.get(harness.model, 99),
            ACCESS_PRIORITY.get(harness.access, 99),
            LEVEL_PRIORITY.get(harness.level, 99),
            harness.harness_id,
        )
    )
    return harnesses


def _commit_and_push(harness_id: str) -> bool:
    del harness_id
    return False


def _repo_head(repo_root: Path) -> str:
    return _capture(["git", "rev-parse", "HEAD"], cwd=repo_root)


def _cleanup_tree_as_root(path: Path) -> None:
    image = env_value("CAD_BENCH_AGENT_IMAGE", root=REPO_ROOT)
    if not image or not path.exists():
        return
    _run(
        [
            "docker",
            "run",
            "--rm",
            "--mount",
            f"type=bind,src={path.parent.resolve()},dst=/cleanup",
            "--entrypoint",
            "rm",
            image,
            "-rf",
            f"/cleanup/{path.name}",
        ],
        cwd=REPO_ROOT,
    )


def _prepare_execution_worktree(
    source_root: Path, benchmark_head: str, *, worker_name: str
) -> Path:
    (source_root / ".tmp").mkdir(parents=True, exist_ok=True)
    temp_root = Path(
        tempfile.mkdtemp(
            prefix=f"cad-bench-matrix-{worker_name}-", dir=str(source_root / ".tmp")
        )
    )
    exec_root = temp_root / "exec"
    add_proc = _run(
        ["git", "worktree", "add", "--detach", str(exec_root), benchmark_head],
        cwd=source_root,
    )
    if add_proc.returncode != 0:
        raise RuntimeError("git worktree add failed")
    env_path = source_root / ".env"
    if env_path.exists():
        (exec_root / ".env").symlink_to(env_path)
    sync_proc = _run(["uv", "sync"], cwd=exec_root)
    if sync_proc.returncode != 0:
        raise RuntimeError("uv sync failed in execution worktree")
    return exec_root


def _remove_execution_worktree(exec_root: Path) -> None:
    temp_root = exec_root.parent
    _run(["git", "worktree", "remove", "--force", str(exec_root)], cwd=REPO_ROOT)
    if temp_root.exists():
        _cleanup_tree_as_root(temp_root)
    shutil.rmtree(temp_root, ignore_errors=True)
    _run(["git", "worktree", "prune"], cwd=REPO_ROOT)


def _existing_run_dirs(exec_root: Path) -> set[Path]:
    logs_root = exec_root / "logs" / "harness_eval"
    if not logs_root.exists():
        return set()
    return {path.resolve() for path in logs_root.iterdir() if path.is_dir()}


def _discover_new_run_dir(exec_root: Path, before: set[Path], harness_id: str) -> Path:
    after = _existing_run_dirs(exec_root)
    created = sorted(after - before)
    if len(created) == 1:
        return created[0]
    if created:
        for path in created:
            report_path = path / "report.json"
            if not report_path.exists():
                continue
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report_harness = ((report.get("harness") or {}) if isinstance(report, dict) else {}).get("id")
            if report_harness == harness_id:
                return path
    raise RuntimeError(f"could not identify local run directory for {harness_id}")


def _approved_entry_for_run(run_dir: Path) -> ApprovedHFReport:
    report_path = run_dir / "report.json"
    if not report_path.exists():
        raise FileNotFoundError(f"missing report.json in {run_dir}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise TypeError(f"report must be a JSON object: {report_path}")
    rows = report.get("rows")
    if not isinstance(rows, list):
        raise TypeError(f"report is missing rows: {report_path}")
    benchmark_task_count = int(report.get("benchmark_task_count") or len(rows))
    if benchmark_task_count < FULL_BENCHMARK_TASK_COUNT:
        raise ValueError(f"partial benchmark cannot be published: {report_path}")
    if len(rows) < benchmark_task_count:
        raise ValueError(f"run is incomplete and cannot be published: {report_path}")
    if len(rows) < FULL_BENCHMARK_TASK_COUNT:
        raise ValueError(f"partial benchmark cannot be published: {report_path}")
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise TypeError(f"report row {index} is not an object: {report_path}")
        metrics = row.get("metrics")
        if not isinstance(metrics, dict):
            raise TypeError(f"report row {index} is missing metrics: {report_path}")
    storage = report.get("storage")
    if not isinstance(storage, dict):
        raise TypeError(f"report is missing storage metadata: {report_path}")
    repo_id = str(storage.get("repo_id") or "").strip()
    run_prefix = str(storage.get("run_prefix") or "").strip("/")
    if not repo_id or not run_prefix:
        raise ValueError(f"report is missing repo_id or run_prefix: {report_path}")
    api = HfApi(token=resolve_hf_token(REPO_ROOT))
    revision = str(api.dataset_info(repo_id=repo_id, token=resolve_hf_token(REPO_ROOT)).sha or "").strip()
    if not revision:
        raise ValueError(f"could not resolve dataset revision for {repo_id}")
    return ApprovedHFReport(
        repo_id=repo_id,
        revision=revision,
        path_in_repo=f"{run_prefix}/report.json",
    )


def _run_harness(exec_root: Path, harness: HarnessRef, task_ids: list[str]) -> tuple[Path | None, int]:
    before = _existing_run_dirs(exec_root)
    cmd = ["uv", "run", "eval-harness", "--harness", harness.ref]
    if task_ids:
        cmd.extend(["--task-ids", ",".join(task_ids)])
    proc = _run(cmd, cwd=exec_root)
    run_dir: Path | None = None
    try:
        run_dir = _discover_new_run_dir(exec_root, before, harness.harness_id)
    except RuntimeError:
        run_dir = None
    return run_dir, proc.returncode


def _worker_loop(
    *,
    worker_name: str,
    benchmark_head: str,
    task_ids: list[str],
    work_queue: queue.Queue[HarnessRef | None],
    result_queue: queue.Queue[HarnessOutcome],
) -> None:
    exec_root = _prepare_execution_worktree(
        REPO_ROOT, benchmark_head, worker_name=worker_name
    )
    try:
        while True:
            harness = work_queue.get()
            if harness is None:
                work_queue.task_done()
                return
            print(f"RUN {harness.harness_id} worker={worker_name}", flush=True)
            run_dir: Path | None = None
            returncode = 1
            run_error = ""
            try:
                run_dir, returncode = _run_harness(exec_root, harness, task_ids)
            except Exception as exc:
                run_error = f"{type(exc).__name__}: {exc}"
            result_queue.put(
                HarnessOutcome(
                    harness=harness,
                    run_dir=run_dir,
                    returncode=returncode,
                    worker_name=worker_name,
                    run_error=run_error,
                )
            )
            work_queue.task_done()
    finally:
        _remove_execution_worktree(exec_root)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the codex/pi matrix and print validated website manifest entries. "
            "This command never edits the public website repo."
        )
    )
    parser.add_argument(
        "--providers",
        default="codex,pi",
        help="Comma-separated provider filter. Default: codex,pi.",
    )
    parser.add_argument(
        "--models",
        default="",
        help="Optional comma-separated model filter.",
    )
    parser.add_argument(
        "--accesses",
        default="",
        help="Optional comma-separated access-mode filter.",
    )
    parser.add_argument(
        "--task-ids",
        default="",
        help="Optional comma-separated task ids. Empty runs the full benchmark.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the selected harnesses without running them.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of benchmark workers to run in parallel. Default: 4.",
    )
    args = parser.parse_args()

    providers = {item.strip() for item in args.providers.split(",") if item.strip()}
    if not providers:
        raise ValueError("at least one provider must be selected")
    unknown_providers = providers - {"codex", "pi"}
    if unknown_providers:
        raise ValueError(f"unsupported providers for this runner: {sorted(unknown_providers)}")

    if not env_value("HF_PROVENANCE_REPO_ID", root=REPO_ROOT):
        raise RuntimeError("HF_PROVENANCE_REPO_ID is required in .env")
    if not env_value("CAD_BENCH_AGENT_IMAGE", root=REPO_ROOT):
        raise RuntimeError("CAD_BENCH_AGENT_IMAGE is required in .env")
    if args.workers < 1:
        raise ValueError("--workers must be at least 1")

    models = {item.strip() for item in args.models.split(",") if item.strip()}
    accesses = {item.strip() for item in args.accesses.split(",") if item.strip()}
    task_ids = [item.strip() for item in args.task_ids.split(",") if item.strip()]
    harnesses = _selected_harnesses(
        providers=providers,
        models=models,
        accesses=accesses,
    )
    if not harnesses:
        raise ValueError("no harnesses matched the requested filters")

    print(f"providers={','.join(sorted(providers))}")
    if models:
        print(f"models={','.join(sorted(models))}")
    if accesses:
        print(f"accesses={','.join(sorted(accesses))}")
    if task_ids:
        print(f"task_ids={','.join(task_ids)}")
    print(f"workers={args.workers}")
    print(f"total_harnesses={len(harnesses)}")
    for harness in harnesses:
        print(f"SELECTED {harness.harness_id}")
    if args.dry_run:
        return 0

    _run_checked(
        ["git", "pull", "--ff-only", "origin", "main"],
        cwd=REPO_ROOT,
        error="git pull --ff-only failed",
    )
    benchmark_head = _repo_head(REPO_ROOT)
    work_queue: queue.Queue[HarnessRef | None] = queue.Queue()
    result_queue: queue.Queue[HarnessOutcome] = queue.Queue()
    threads: list[threading.Thread] = []
    for worker_index in range(min(args.workers, len(harnesses))):
        worker_name = f"w{worker_index + 1}"
        thread = threading.Thread(
            target=_worker_loop,
            kwargs={
                "worker_name": worker_name,
                "benchmark_head": benchmark_head,
                "task_ids": task_ids,
                "work_queue": work_queue,
                "result_queue": result_queue,
            },
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    for harness in harnesses:
        work_queue.put(harness)
    for _ in threads:
        work_queue.put(None)
    failures: list[str] = []
    try:
        for _ in harnesses:
            outcome = result_queue.get()
            harness = outcome.harness
            run_dir = outcome.run_dir
            returncode = outcome.returncode
            validation_error = ""
            manual_entry = ""
            should_publish = (
                run_dir is not None and returncode == 0 and not outcome.run_error
            )
            if should_publish:
                try:
                    entry = _approved_entry_for_run(run_dir)
                    manual_entry = json.dumps(entry.to_json(), sort_keys=True)
                except Exception as exc:
                    validation_error = f"{type(exc).__name__}: {exc}"
            committed = _commit_and_push(harness.harness_id)
            print(
                f"DONE {harness.harness_id} run_dir={(run_dir.name if run_dir else 'missing')} "
                f"worker={outcome.worker_name} "
                f"website=manual-only "
                f"committed={'yes' if committed else 'no-change'} "
                f"returncode={returncode}"
                + (f" run_error={outcome.run_error}" if outcome.run_error else "")
                + (f" validation_error={validation_error}" if validation_error else "")
                + (f" manual_entry={manual_entry}" if manual_entry else ""),
                flush=True,
            )
            if returncode != 0 or run_dir is None or outcome.run_error or validation_error:
                failures.append(
                    f"{harness.harness_id}: returncode={returncode} "
                    f"run_dir={'missing' if run_dir is None else run_dir.name}"
                    + (f" run_error={outcome.run_error}" if outcome.run_error else "")
                    + (f" validation_error={validation_error}" if validation_error else "")
                )
    finally:
        work_queue.join()
        for thread in threads:
            thread.join()
    if failures:
        for failure in failures:
            print(f"FAILED {failure}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
