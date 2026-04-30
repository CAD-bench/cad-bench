from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from bench.config import (
    DIFFICULTY_WEIGHTS,
    env_value,
    required_env_value,
    summarize_difficulty_aggregates,
)
from bench.provenance import (
    HFProvenanceStore,
    load_hf_provenance_config,
    resolve_hf_token,
    write_docker_bundle,
    write_provenance_bundle,
)
from bench.tasks_repo import export_task_canaries_main, upload_public_tasks_main
from harnesses.costs import usage_with_cost
from harnesses import utils as harness_utils
from tasks import specs as task_specs
from tasks import utils as task_utils

TASK_DEFS = task_specs.load_all_task_specs()
TASK_MODULES = TASK_DEFS
TASK_IDS = [task.task_id for task in TASK_DEFS.values()]
CONTAINER_EXPORT_LOCK_PATH = Path("/tmp/cad-bench-container-export.lock")

builtin_harness_name = harness_utils.builtin_harness_name
builtin_harness_ref = harness_utils.builtin_harness_ref
build_step_prompt = harness_utils.build_step_prompt
compose_step_submission_prompt = harness_utils.compose_step_submission_prompt
generate_build123d_docs_bundle = harness_utils.generate_build123d_docs_bundle
load_harness_spec = harness_utils.load_harness_spec


def _evaluate_for_task(code: str, task_id: str) -> dict[str, Any]:
    return TASK_DEFS[task_id].evaluate_code(code, llm_generated=False).metrics


@lru_cache(maxsize=2048)
def _evaluate_for_task_cached(task_id: str, code: str) -> dict[str, Any]:
    return _evaluate_for_task(code, task_id)


@lru_cache(maxsize=2048)
def _evaluate_code_for_task_cached(task_id: str, code: str) -> dict[str, Any]:
    return _evaluate_for_task_cached(task_id, code)


def extract_code(completion: Any) -> str:
    parsed = harness_utils.CADCodeParser().parse_answer(completion)
    return parsed if isinstance(parsed, str) else ""


def score_code_submission(
    task_id: str, code: str, *, llm_generated: bool = True
) -> dict[str, Any]:
    if not code.strip():
        return TASK_DEFS[task_id].evaluate_code("", llm_generated=False).metrics
    if llm_generated:
        with task_utils.llm_generated_code_context():
            return _evaluate_code_for_task_cached(task_id, code)
    return _evaluate_code_for_task_cached(task_id, code)


def evaluate_step_submission(
    task_id: str, step_path: Path, *, provenance_dir: Path | None = None
) -> task_specs.TaskEvaluation:
    return TASK_DEFS[task_id](step_path, provenance_dir=provenance_dir)


def score_step_submission(
    task_id: str, step_path: Path, *, provenance_dir: Path | None = None
) -> dict[str, Any]:
    return evaluate_step_submission(
        task_id, step_path, provenance_dir=provenance_dir
    ).metrics


def load_benchmark(
    system_prompt: str | None = harness_utils.XML_CODE_SYSTEM_PROMPT,
    task_ids: list[str] | None = None,
) -> task_specs.BenchmarkDefinition:
    selected = task_ids or list(TASK_IDS)
    unknown = [task_id for task_id in selected if task_id not in TASK_DEFS]
    if unknown:
        raise ValueError(f"Unknown task ids: {unknown}")
    examples = tuple(
        {
            "question": harness_utils.compose_single_turn_prompt(
                str(TASK_DEFS[task_id].prompt)
            ),
            "answer": {"task_id": task_id},
            "info": {
                "task_id": task_id,
                "difficulty": str(TASK_DEFS[task_id].difficulty),
                "mode": "single_turn",
            },
            "task": "cad-build123d-bench",
        }
        for task_id in selected
    )
    return task_specs.BenchmarkDefinition(
        name="cad-build123d-bench",
        mode="single_turn",
        system_prompt=system_prompt,
        parser=harness_utils.CADCodeParser(),
        examples=examples,
    )


def render_candidate_views(
    code: str, output_dir: str | Path, prefix: str = "candidate"
) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    part = task_utils.execute_candidate_code(code)
    mesh = task_utils.mesh_from_part(part)

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    vertices = mesh.vertices
    faces = mesh.faces
    views = [(20, 35, "iso"), (0, 90, "front"), (90, 0, "top")]
    written: list[Path] = []

    for elev, azim, name in views:
        fig = plt.figure(figsize=(4, 4), dpi=180)
        ax = fig.add_subplot(111, projection="3d")
        ax.plot_trisurf(
            vertices[:, 0],
            vertices[:, 1],
            vertices[:, 2],
            triangles=faces,
            color="#b8bec8",
            linewidth=0.05,
            edgecolor="#6f7782",
            antialiased=True,
            shade=True,
        )
        mins = vertices.min(axis=0)
        maxs = vertices.max(axis=0)
        center = (mins + maxs) / 2.0
        span = float((maxs - mins).max()) / 2.0
        ax.set_xlim(center[0] - span, center[0] + span)
        ax.set_ylim(center[1] - span, center[1] + span)
        ax.set_zlim(center[2] - span, center[2] + span)
        ax.view_init(elev=elev, azim=azim)
        ax.set_axis_off()
        out_file = output_path / f"{prefix}_{name}.png"
        fig.savefig(out_file, bbox_inches="tight", pad_inches=0)
        plt.close(fig)
        written.append(out_file)
    return written


def render_views_main() -> int:
    parser = argparse.ArgumentParser(
        description="Render candidate Build123D code into fixed PNG views."
    )
    parser.add_argument(
        "code_file", help="Path to a text file containing raw Build123D code"
    )
    parser.add_argument("--out", default="./renders", help="Output directory for PNGs")
    parser.add_argument("--prefix", default="candidate", help="Output file prefix")
    args = parser.parse_args()
    code = Path(args.code_file).read_text(encoding="utf-8")
    for file_path in render_candidate_views(code, args.out, args.prefix):
        print(file_path)
    return 0


def _render_functional_video(task_id: str, out_dir: Path) -> tuple[Path, Path]:
    return TASK_DEFS[task_id].render_reference_video(out_dir)


def export_task_media_main() -> int:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.image as mpimg
    import matplotlib.pyplot as plt
    import numpy as np

    def slug(name: str) -> str:
        return name.replace("/", "_").replace(" ", "_")

    def write_view_sheet(
        task_id: str, title: str, source_images: list[Path], out_path: Path
    ) -> None:
        fig, axes = plt.subplots(1, len(source_images), figsize=(12, 4), dpi=180)
        if isinstance(axes, np.ndarray):
            axes = list(axes.ravel())
        elif not isinstance(axes, (list, tuple)):
            axes = [axes]
        labels = ["iso", "front", "top"][: len(source_images)]
        for ax, label, image_path in zip(axes, labels, source_images, strict=True):
            ax.imshow(mpimg.imread(image_path))
            ax.set_title(label, fontsize=10)
            ax.axis("off")
        fig.suptitle(title, fontsize=12)
        fig.tight_layout()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_path, bbox_inches="tight", pad_inches=0.12)
        plt.close(fig)

    def render_still_bundle(task_id: str, code: str, out_dir: Path, title: str) -> Path:
        raw_dir = out_dir / "_raw_views" / slug(task_id)
        written = render_candidate_views(code, raw_dir, prefix=slug(task_id))
        out_path = out_dir / f"cad_bench__{slug(task_id)}.png"
        write_view_sheet(task_id, title, written, out_path)
        return out_path

    def build_manifest(out_dir: Path, records: list[dict[str, Any]]) -> Path:
        manifest = {"generated_at": datetime.now().isoformat(), "records": records}
        manifest_path = out_dir / "cad_bench__manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return manifest_path

    def tailscale_send(files: list[Path], target: str) -> None:
        subprocess.run(
            [
                "tailscale",
                "file",
                "cp",
                *[path.as_posix() for path in files],
                f"{target}:",
            ],
            check=True,
        )

    parser = argparse.ArgumentParser(
        description="Render stills and Blender videos for benchmark reference tasks."
    )
    parser.add_argument(
        "--out", default="outputs/cad_benchmark_media", help="Output directory"
    )
    parser.add_argument(
        "--skip-send",
        action="store_true",
        help="Do not send outputs over Tailscale file transfer",
    )
    parser.add_argument("--target", help="Tailscale target for file transfer")
    parser.add_argument(
        "--task",
        action="append",
        default=[],
        help="Only render the specified task id; may be repeated",
    )
    args = parser.parse_args()
    if not args.skip_send and not args.target:
        raise SystemExit("--target is required unless --skip-send is set")

    out_dir = Path(args.out).resolve()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    selected_tasks = list(args.task) if args.task else list(TASK_DEFS.keys())
    unknown = [task_id for task_id in selected_tasks if task_id not in TASK_DEFS]
    if unknown:
        raise SystemExit(f"unknown task ids: {', '.join(unknown)}")

    records: list[dict[str, Any]] = []
    files_to_send: list[Path] = []
    for task_id in selected_tasks:
        spec = TASK_DEFS[task_id]
        image_path = render_still_bundle(
            task_id, spec.reference_solution_code, out_dir, task_id
        )
        files_to_send.append(image_path)
        record = {"task_id": task_id, "image": image_path.name}
        if spec.has_functional_video:
            video_path, sim_path = _render_functional_video(task_id, out_dir)
            files_to_send.extend([video_path, sim_path])
            record["video"] = video_path.name
            record["simulation"] = sim_path.name
        records.append(record)

    manifest_path = build_manifest(out_dir, records)
    files_to_send.append(manifest_path)
    if not args.skip_send:
        tailscale_send(files_to_send, args.target)
    return 0


HF_HARNESS_PATH_PREFIX = "evals/harnesses"
TRANSIENT_AGENT_ERROR_MARKERS = (
    "server_error",
    "rate_limit",
    "rate limit",
    "temporarily unavailable",
    "overloaded",
)


@dataclass(frozen=True)
class BindMount:
    source: Path
    target: str
    read_only: bool = False


def _join_errors(*parts: str | None) -> str | None:
    values = [str(part).strip() for part in parts if str(part or "").strip()]
    if not values:
        return None
    return " | ".join(values)


def _agent_error_messages(events: list[dict[str, Any]]) -> list[str]:
    messages: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        message = str(event.get("errorMessage") or "").strip()
        if message:
            messages.append(message)
    return messages


def _is_transient_agent_error(
    spec: harness_utils.HarnessSpec,
    run_result: harness_utils.AgentRunResult,
) -> bool:
    if spec.provider not in {"pi", "codex"}:
        return False
    haystacks = [
        str(run_result.error or ""),
        run_result.stdout,
        run_result.stderr,
        *_agent_error_messages(run_result.parsed_events),
    ]
    normalized = "\n".join(part.lower() for part in haystacks if part).strip()
    return any(marker in normalized for marker in TRANSIENT_AGENT_ERROR_MARKERS)


def _reset_directory_contents(path: Path) -> None:
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _write_attempt_artifacts(
    attempt_dir: Path,
    *,
    run_result: harness_utils.AgentRunResult,
    container_export_path: Path,
) -> None:
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (attempt_dir / "stdout.txt").write_text(
        run_result.stdout, encoding="utf-8", errors="ignore"
    )
    (attempt_dir / "stderr.txt").write_text(
        run_result.stderr, encoding="utf-8", errors="ignore"
    )
    (attempt_dir / "response.txt").write_text(
        run_result.response, encoding="utf-8", errors="ignore"
    )
    _write_json(attempt_dir / "events.parsed.json", run_result.parsed_events)
    _write_json(attempt_dir / "command.json", run_result.command)
    _write_json(attempt_dir / "session.json", {"session_id": run_result.session_id})
    _write_json(attempt_dir / "usage.json", run_result.usage)
    error_payload = {
        "error": run_result.error,
        "event_errors": _agent_error_messages(run_result.parsed_events),
        "returncode": run_result.returncode,
    }
    _write_json(attempt_dir / "error.json", error_payload)
    status_path = _container_export_status_path(container_export_path)
    if container_export_path.exists():
        container_export_path.replace(attempt_dir / "container_image_after.tar")
    if status_path.exists():
        status_path.replace(attempt_dir / status_path.name)


def _container_provenance_error(
    status: dict[str, Any] | None, container_after_url: str | None
) -> str:
    if not isinstance(status, dict):
        return "missing post-run container provenance status"
    if str(status.get("export_error") or "").strip():
        return f"post-run container export failed: {status['export_error']}"
    if str(status.get("cleanup_error") or "").strip():
        return f"post-run container cleanup failed: {status['cleanup_error']}"
    required_flags = (
        "container_found_after_run",
        "committed_image",
        "saved_image",
        "removed_container",
        "exported",
    )
    missing = [name for name in required_flags if status.get(name) is not True]
    if missing:
        return "incomplete post-run container provenance: " + ", ".join(missing)
    if not str(container_after_url or "").strip():
        return "missing uploaded post-run container image"
    return ""


def _include_container_image_provenance(spec: harness_utils.HarnessSpec) -> bool:
    return spec.provider in {"codex", "pi"}


def _save_post_run_container_image(spec: harness_utils.HarnessSpec) -> bool:
    return _include_container_image_provenance(spec)


def default_container_image() -> str:
    return required_env_value("CAD_BENCH_AGENT_IMAGE")


def ensure_docker_image_available(image: str) -> None:
    proc = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (
            proc.stderr.strip() or proc.stdout.strip() or "docker image inspect failed"
        )
        raise RuntimeError(f"Docker image '{image}' is not available: {detail}")


def docker_run_command(
    *,
    image: str,
    entrypoint: str,
    mounts: list[BindMount],
    env: dict[str, str],
    args: list[str],
    workdir: str = harness_utils.CONTAINER_WORKDIR,
    remove: bool = True,
    container_name: str = "",
    network: str = "bridge",
) -> list[str]:
    cmd = [
        "docker",
        "run",
    ]
    if remove:
        cmd.append("--rm")
    if container_name:
        cmd.extend(["--name", container_name])
    cmd.extend(
        [
            "--init",
            "--network",
            network,
            "--workdir",
            workdir,
            "--entrypoint",
            entrypoint,
        ]
    )
    for key, value in env.items():
        cmd.extend(["-e", f"{key}={value}"])
    for mount in mounts:
        spec = f"type=bind,src={mount.source},dst={mount.target}"
        if mount.read_only:
            spec += ",readonly"
        cmd.extend(["--mount", spec])
    cmd.append(image)
    cmd.extend(args)
    return cmd


def _completed_process_payload(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    args = proc.args
    if isinstance(args, (list, tuple)):
        command = harness_utils.sanitize_command_for_logging(
            [str(item) for item in args]
        )
    else:
        command = [str(args)]
    return {
        "args": command,
        "returncode": int(proc.returncode),
        "stdout": proc.stdout if isinstance(proc.stdout, str) else "",
        "stderr": proc.stderr if isinstance(proc.stderr, str) else "",
    }


def _sanitize_container_inspect_payload(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        return text
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for key, item in value.items():
                if key == "Env" and isinstance(item, list):
                    sanitized[key] = [
                        harness_utils.sanitize_env_var_spec(str(entry))
                        for entry in item
                    ]
                    continue
                sanitized[key] = scrub(item)
            return sanitized
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    return json.dumps(scrub(payload), indent=2) + "\n"


def _completed_process_payload_for_stage(
    stage: str, proc: subprocess.CompletedProcess[str]
) -> dict[str, Any]:
    payload = _completed_process_payload(proc)
    if stage == "inspect_container":
        payload["stdout"] = _sanitize_container_inspect_payload(payload["stdout"])
        payload["stderr"] = _sanitize_container_inspect_payload(payload["stderr"])
    return payload


def _container_export_status_path(container_export_path: Path) -> Path:
    return container_export_path.with_suffix(
        container_export_path.suffix + ".status.json"
    )


def _lock_container_export() -> tuple[Path, Any]:
    CONTAINER_EXPORT_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = CONTAINER_EXPORT_LOCK_PATH.open("w", encoding="utf-8")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    return CONTAINER_EXPORT_LOCK_PATH, lock_file


def _summarize_container_export_status(
    status: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(status, dict):
        return None
    steps = status.get("steps")
    summarized_steps: list[dict[str, Any]] = []
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            summarized_steps.append(
                {
                    "stage": step.get("stage"),
                    "returncode": step.get("returncode"),
                }
            )
    run = status.get("run")
    run_returncode = None
    if isinstance(run, dict):
        run_returncode = run.get("returncode")
    return {
        "container_found_after_run": status.get("container_found_after_run"),
        "committed_image": status.get("committed_image"),
        "saved_image": status.get("saved_image"),
        "removed_image": status.get("removed_image"),
        "removed_container": status.get("removed_container"),
        "exported": status.get("exported"),
        "export_size_bytes": status.get("export_size_bytes"),
        "export_error": status.get("export_error"),
        "cleanup_error": status.get("cleanup_error"),
        "run_returncode": run_returncode,
        "steps": summarized_steps,
    }


def run_docker_command(
    *,
    image: str,
    entrypoint: str,
    mounts: list[BindMount],
    env: dict[str, str],
    args: list[str],
    workdir: str = harness_utils.CONTAINER_WORKDIR,
    container_export_path: Path | None = None,
    container_name: str = "",
    network: str = "bridge",
) -> subprocess.CompletedProcess[str]:
    remove = container_export_path is None
    cmd = docker_run_command(
        image=image,
        entrypoint=entrypoint,
        mounts=mounts,
        env=env,
        args=args,
        workdir=workdir,
        remove=remove,
        container_name=container_name,
        network=network,
    )
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if container_export_path is None:
        return proc
    if not container_name:
        raise ValueError(
            "container_name is required when saving the post-run container image"
        )
    container_export_path.parent.mkdir(parents=True, exist_ok=True)
    status = {
        "container_name": container_name,
        "container_export_path": str(container_export_path),
        "run": _completed_process_payload(proc),
        "container_found_after_run": False,
        "committed_image": False,
        "saved_image": False,
        "removed_image": False,
        "removed_container": False,
        "exported": False,
        "export_size_bytes": 0,
        "export_error": None,
        "cleanup_error": None,
        "steps": [],
    }

    def record_step(
        stage: str, step_proc: subprocess.CompletedProcess[str]
    ) -> subprocess.CompletedProcess[str]:
        status["steps"].append(
            {"stage": stage, **_completed_process_payload_for_stage(stage, step_proc)}
        )
        return step_proc

    after_image = f"cad-bench-after:{container_name.lower()}"
    export_lock_file = None
    try:
        lock_start = time.monotonic()
        _lock_path, export_lock_file = _lock_container_export()
        status["steps"].append(
            {
                "stage": "export_lock",
                "returncode": 0,
                "wait_s": round(time.monotonic() - lock_start, 3),
            }
        )
        inspect_proc = record_step(
            "inspect_container",
            subprocess.run(
                ["docker", "container", "inspect", container_name],
                capture_output=True,
                text=True,
                check=False,
            ),
        )
        status["container_found_after_run"] = inspect_proc.returncode == 0
        if inspect_proc.returncode != 0:
            detail = inspect_proc.stderr.strip() or inspect_proc.stdout.strip()
            status["export_error"] = detail or "post-run container not found"
            return proc
        commit_error = ""
        for attempt in range(1, 4):
            commit_proc = record_step(
                f"commit_image_attempt_{attempt}",
                subprocess.run(
                    ["docker", "commit", container_name, after_image],
                    capture_output=True,
                    text=True,
                    check=False,
                ),
            )
            status["committed_image"] = commit_proc.returncode == 0
            if commit_proc.returncode == 0:
                break
            detail = commit_proc.stderr.strip() or commit_proc.stdout.strip()
            commit_error = detail or "docker commit failed"
            if attempt < 3:
                time.sleep(attempt)
        if not status["committed_image"]:
            status["export_error"] = commit_error or "docker commit failed"
            return proc
        save_output_path = container_export_path.with_name(
            container_export_path.name + ".tmp"
        )
        save_error = ""
        for attempt in range(1, 4):
            save_output_path.unlink(missing_ok=True)
            save_proc = record_step(
                f"save_image_attempt_{attempt}",
                subprocess.run(
                    ["docker", "save", "--output", str(save_output_path), after_image],
                    capture_output=True,
                    text=True,
                    check=False,
                ),
            )
            if save_proc.returncode == 0:
                save_output_path.replace(container_export_path)
                status["saved_image"] = True
                break
            detail = save_proc.stderr.strip() or save_proc.stdout.strip()
            save_error = detail or "docker save failed"
            save_output_path.unlink(missing_ok=True)
            if attempt < 3:
                time.sleep(attempt)
        if not status["saved_image"]:
            status["export_error"] = save_error or "docker save failed"
        image_rm_proc = record_step(
            "remove_image",
            subprocess.run(
                ["docker", "image", "rm", after_image],
                capture_output=True,
                text=True,
                check=False,
            ),
        )
        status["removed_image"] = image_rm_proc.returncode == 0
        if image_rm_proc.returncode != 0 and status["cleanup_error"] is None:
            detail = image_rm_proc.stderr.strip() or image_rm_proc.stdout.strip()
            status["cleanup_error"] = detail or "docker image rm failed"
        return proc
    finally:
        remove_proc = record_step(
            "remove_container",
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                text=True,
                check=False,
            ),
        )
        status["removed_container"] = remove_proc.returncode == 0
        if remove_proc.returncode != 0 and status["cleanup_error"] is None:
            detail = remove_proc.stderr.strip() or remove_proc.stdout.strip()
            status["cleanup_error"] = detail or "docker rm failed"
        if container_export_path.exists():
            status["exported"] = True
            status["export_size_bytes"] = container_export_path.stat().st_size
        if export_lock_file is not None:
            export_lock_file.close()
        _write_json(_container_export_status_path(container_export_path), status)


_EXPORT_HELPER = """
import json
import sys
from pathlib import Path

from build123d import export_step

from tasks import utils as task_utils

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
part = task_utils.execute_llm_generated_code(str(payload["code"]))
step_path = Path(payload["step_path"])
step_path.parent.mkdir(parents=True, exist_ok=True)
export_step(part, step_path)
"""


def _export_submission_step_result(
    task_id: str, code: str, step_path: Path, *, image: str = ""
) -> dict[str, Any]:
    if not code.strip():
        return {"error": "candidate code is empty", "process": None}
    payload_path = step_path.parent / ".export_payload.json"
    with ExitStack() as stack:
        stack.callback(lambda: payload_path.exists() and payload_path.unlink())
        export_step_path = (
            f"{CONTAINER_SUBMISSION_DIR}/{step_path.name}" if image else str(step_path)
        )
        payload_path.write_text(
            json.dumps(
                {"task_id": task_id, "code": code, "step_path": export_step_path}
            ),
            encoding="utf-8",
        )
        if image:
            proc = run_docker_command(
                image=image,
                entrypoint="uv",
                mounts=[
                    BindMount(step_path.parent.resolve(), CONTAINER_SUBMISSION_DIR)
                ],
                env={},
                args=[
                    "run",
                    "--no-sync",
                    "python",
                    "-c",
                    _EXPORT_HELPER,
                    f"{CONTAINER_SUBMISSION_DIR}/{payload_path.name}",
                ],
                workdir=CONTAINER_APP_DIR,
                network="none",
            )
        else:
            proc = subprocess.run(
                [sys.executable, "-c", _EXPORT_HELPER, str(payload_path)],
                capture_output=True,
                text=True,
                check=False,
            )
    error = ""
    if proc.returncode != 0:
        error = (
            proc.stderr.strip()
            or proc.stdout.strip()
            or f"step export failed with exit {proc.returncode}"
        )
    return {
        "error": error,
        "process": _completed_process_payload(proc),
    }


CONTAINER_APP_DIR = harness_utils.CONTAINER_APP_DIR
CONTAINER_DOCS_DIR = harness_utils.CONTAINER_DOCS_DIR
CONTAINER_HOME_STEP_PATH = harness_utils.CONTAINER_HOME_STEP_PATH
CONTAINER_SUBMISSION_DIR = harness_utils.CONTAINER_SUBMISSION_DIR
CONTAINER_WORKDIR = harness_utils.CONTAINER_WORKDIR


def select_task_specs(
    task_ids: list[str] | None = None,
) -> list[task_specs.TaskModuleSpec]:
    selected = task_ids or list(TASK_IDS)
    unknown = [task_id for task_id in selected if task_id not in TASK_DEFS]
    if unknown:
        raise ValueError(f"Unknown task ids: {unknown}")
    return [TASK_DEFS[task_id] for task_id in selected]


def build_harness_prompt(
    spec: harness_utils.HarnessSpec, task_prompt: str
) -> tuple[str | None, str]:
    module = harness_utils.harness_module(spec)
    builder = getattr(module, "build_prompt", None)
    if not callable(builder):
        raise AttributeError(
            f"harness module {spec.file_path} must define callable `build_prompt`"
        )
    return builder(spec, task_prompt)


def run_harness(
    spec: harness_utils.HarnessSpec,
    *,
    system_prompt: str | None,
    prompt: str,
    workdir: Path,
    submission_dir: Path | None,
    image: str,
    docs_dir: Path | None = None,
    container_export_path: Path | None = None,
    container_name: str = "",
) -> harness_utils.AgentRunResult:
    module = harness_utils.harness_module(spec)
    runner = getattr(module, "run", None)
    if not callable(runner):
        raise AttributeError(
            f"harness module {spec.file_path} must define callable `run`"
        )
    return runner(
        runtime=sys.modules[__name__],
        spec=spec,
        system_prompt=system_prompt,
        prompt=prompt,
        workdir=workdir,
        submission_dir=submission_dir,
        image=image,
        docs_dir=docs_dir,
        container_export_path=container_export_path,
        container_name=container_name,
    )


def _summarize_difficulty_aggregates(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, float], dict[str, int], float]:
    return summarize_difficulty_aggregates(rows)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def score_from_step(
    task_id: str, step_path: Path, *, provenance_dir: Path | None = None
) -> dict[str, float]:
    submission_exists = (
        1.0 if step_path.exists() and step_path.stat().st_size > 0 else 0.0
    )
    try:
        if provenance_dir is None:
            raw = score_step_submission(task_id, step_path)
        else:
            raw = score_step_submission(
                task_id,
                step_path,
                provenance_dir=provenance_dir,
            )
    except ValueError as exc:
        return {
            "submission_exists": submission_exists,
            "build_success": 0.0,
            "reward": 0.0,
            "overall_score": 0.0,
            "task_score": 0.0,
            "score_error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "submission_exists": submission_exists,
        "build_success": float(raw["build_success"]),
        "reward": float(raw["reward"]),
        "overall_score": float(raw.get("overall_score", raw["task_score"])),
        "task_score": float(raw["task_score"]),
        "score_error": str(raw["error_message"]),
    }


def summarize_harness(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_difficulty, _, benchmark_score = _summarize_difficulty_aggregates(rows)

    return {
        "benchmark_score": benchmark_score,
        "difficulty_weights": dict(DIFFICULTY_WEIGHTS),
        "by_difficulty": by_difficulty,
    }


def docs_cli() -> None:
    parser = argparse.ArgumentParser(
        description="Generate local Build123D docs bundle."
    )
    parser.add_argument(
        "--out", default=str(harness_utils.DOCS_CACHE_DIR), help="Output docs directory"
    )
    parser.add_argument(
        "--top-only",
        action="store_true",
        help="Only fetch the smaller curated core docs set (faster).",
    )
    args = parser.parse_args()
    out = harness_utils.generate_build123d_docs_bundle(
        Path(args.out), include_submodules=not args.top_only
    )
    print(out.as_posix())


def eval_harness_main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate one harness spec against the CAD benchmark."
    )
    parser.add_argument(
        "--harness", required=True, help="Path to a harness Python module"
    )
    parser.add_argument(
        "--task-ids", default="", help="Comma-separated task ids. Empty=all."
    )
    args = parser.parse_args()
    agent_image = default_container_image()

    spec = harness_utils.load_harness_spec(args.harness)
    if spec.provider == "openai":
        if not env_value("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is required in .env")
    elif spec.provider in {"codex", "pi", "openai_codex"}:
        from harnesses.codex.utils import has_codex_auth_token

        if not has_codex_auth_token():
            raise RuntimeError("CODEX_AUTH_JSON_B64 is required in .env")
    ensure_docker_image_available(agent_image)

    repo_root = Path.cwd()
    provenance_config = load_hf_provenance_config(repo_root)
    task_filter = [x.strip() for x in args.task_ids.split(",") if x.strip()]
    tasks = select_task_specs(task_ids=task_filter or None)
    git_head_proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    git_head = git_head_proc.stdout.strip() if git_head_proc.returncode == 0 else ""

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_slug = re.sub(
        r"[^a-zA-Z0-9_.-]+", "_", spec.harness_id.replace("/", "__")
    ).strip("_")
    run_name = f"{run_slug}_{stamp}_{os.getpid()}"
    run_prefix = f"{HF_HARNESS_PATH_PREFIX}/{run_name}"
    storage = HFProvenanceStore(
        repo_id=provenance_config.repo_id,
        path_prefix=run_prefix,
        token=resolve_hf_token(repo_root),
    )
    out_root = repo_root / "logs" / "harness_eval" / run_name
    out_root.mkdir(parents=True, exist_ok=True)

    metadata_dir = out_root / "metadata"
    metadata_files = write_provenance_bundle(metadata_dir, repo_root)
    if _include_container_image_provenance(spec):
        metadata_files.update(
            write_docker_bundle(metadata_dir / "docker", repo_root, agent_image)
        )
    harness_copy = metadata_dir / "harness.py"
    harness_copy.write_text(
        Path(spec.file_path).read_text(encoding="utf-8"), encoding="utf-8"
    )
    metadata_files["harness"] = "harness.py"
    harness_ref = metadata_dir / "harness_ref.txt"
    harness_ref.write_text(
        harness_utils.format_harness_ref(Path(spec.file_path), spec.symbol_name) + "\n",
        encoding="utf-8",
    )
    metadata_files["harness_ref"] = "harness_ref.txt"
    metadata_dir_url = storage.tree_url("metadata")

    report: dict[str, Any] = {
        "timestamp_utc": stamp,
        "agent_image": agent_image,
        "local_run_dir": str(out_root),
        "harness": {
            "id": spec.harness_id,
            "provider": spec.provider,
            "strategy": spec.strategy,
            "model": spec.model,
            "reasoning_effort": spec.reasoning_effort,
            "access": spec.access,
            "file_path": spec.file_path,
            "symbol_name": spec.symbol_name,
        },
        "tasks": [task.task_id for task in tasks],
        "git_head": git_head,
        "difficulty_weights": dict(DIFFICULTY_WEIGHTS),
        "storage": {
            "backend": "huggingface_dataset",
            "repo_id": provenance_config.repo_id,
            "run_prefix": run_prefix,
            "run_url": storage.tree_url(""),
            "encryption": "none",
            "metadata_dir": metadata_dir_url,
            "metadata_files": metadata_files,
            "report_file": storage.file_url("report.json"),
        },
    }

    rows: list[dict[str, Any]] = []
    prompt_manifest: list[dict[str, str]] = []
    for idx, task in enumerate(tasks, start=1):
        task_root = out_root / "tasks" / task.task_id
        grading_dir = task_root / "grading"
        workdir = task_root / "workdir"
        submission_dir = task_root / "submission"
        submission_step = submission_dir / "final.step"
        container_export_path = task_root / "container_after.tar"
        task_container_export_path = (
            container_export_path if _save_post_run_container_image(spec) else None
        )
        container_name = re.sub(
            r"[^a-zA-Z0-9_.-]+", "-", f"cad-bench-{run_name}-{idx}"
        ).strip("-")[:120]
        task_root.mkdir(parents=True, exist_ok=True)
        grading_dir.mkdir(parents=True, exist_ok=True)
        workdir.mkdir(parents=True, exist_ok=True)
        submission_dir.mkdir(parents=True, exist_ok=True)
        submission_step.touch()

        system_prompt, prompt = build_harness_prompt(spec, task.prompt)
        docs_dir: Path | None = None
        if harness_utils.needs_offline_docs(spec):
            docs_dir = harness_utils.ensure_build123d_docs_bundle()

        start = time.time()
        max_task_attempts = max(
            1, int(env_value("CAD_BENCH_AGENT_TASK_ATTEMPTS", "3") or "3")
        )
        retry_errors: list[str] = []
        attempt_count = 0
        while True:
            attempt_count += 1
            attempt_container_name = container_name
            if attempt_count > 1:
                attempt_container_name = f"{container_name}-r{attempt_count}"[:120]
            run_result = run_harness(
                spec,
                system_prompt=system_prompt,
                prompt=prompt,
                workdir=workdir,
                submission_dir=submission_dir,
                image=agent_image,
                docs_dir=docs_dir,
                container_export_path=task_container_export_path,
                container_name=attempt_container_name,
            )
            if (
                attempt_count >= max_task_attempts
                or not _is_transient_agent_error(spec, run_result)
            ):
                break
            attempt_dir = task_root / "attempts" / f"attempt_{attempt_count}"
            _write_attempt_artifacts(
                attempt_dir,
                run_result=run_result,
                container_export_path=container_export_path,
            )
            retry_detail = _join_errors(
                run_result.error,
                "; ".join(_agent_error_messages(run_result.parsed_events)),
            ) or "transient agent error"
            retry_errors.append(retry_detail)
            print(
                f"[{spec.harness_id}] {idx}/{len(tasks)} {task.task_id}: "
                f"retrying transient provider error on attempt {attempt_count}/{max_task_attempts}: "
                f"{retry_detail}",
                flush=True,
            )
            _reset_directory_contents(workdir)
            _reset_directory_contents(submission_dir)
            submission_step.touch()
            if task_container_export_path is not None:
                task_container_export_path.unlink(missing_ok=True)
                _container_export_status_path(task_container_export_path).unlink(
                    missing_ok=True
                )
            time.sleep(min(30, 5 * attempt_count))

        code = ""
        export_result: dict[str, Any] = {"error": "", "process": None}
        if spec.strategy == "one_shot_code":
            code = extract_code([{"role": "assistant", "content": run_result.response}])
            if code:
                export_result = _export_submission_step_result(
                    task.task_id, code, submission_step, image=agent_image
                )
        export_error = str(export_result.get("error") or "")
        elapsed = time.time() - start

        prompt_file = task_root / "prompt.txt"
        system_prompt_file = task_root / "system_prompt.txt"
        stdout_file = task_root / "stdout.txt"
        stderr_file = task_root / "stderr.txt"
        response_file = task_root / "response.txt"
        events_file = task_root / "events.jsonl"
        parsed_events_file = task_root / "events.parsed.json"
        command_file = task_root / "command.json"
        session_file = task_root / "session.json"
        usage_file = task_root / "usage.json"
        code_file = task_root / "code.py"
        export_process_file = task_root / "export_process.json"
        prompt_file.write_text(prompt, encoding="utf-8")
        system_prompt_file.write_text(system_prompt or "", encoding="utf-8")
        prompt_manifest.append(
            {
                "task_id": task.task_id,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "system_prompt_sha256": hashlib.sha256(
                    (system_prompt or "").encode("utf-8")
                ).hexdigest(),
            }
        )
        stdout_file.write_text(run_result.stdout, encoding="utf-8", errors="ignore")
        stderr_file.write_text(run_result.stderr, encoding="utf-8", errors="ignore")
        response_file.write_text(run_result.response, encoding="utf-8", errors="ignore")
        events_file.write_text(run_result.stdout, encoding="utf-8", errors="ignore")
        _write_json(parsed_events_file, run_result.parsed_events)
        _write_json(command_file, run_result.command)
        _write_json(session_file, {"session_id": run_result.session_id})
        _write_json(usage_file, run_result.usage)
        if isinstance(export_result.get("process"), dict):
            _write_json(export_process_file, export_result["process"])
        if code:
            code_file.write_text(code, encoding="utf-8")
        extra_artifact_paths: dict[str, str] = {}
        for artifact_name, content in sorted(run_result.artifact_contents.items()):
            if (
                not artifact_name
                or Path(artifact_name).name != artifact_name
                or Path(artifact_name).anchor
            ):
                raise ValueError(f"invalid artifact file name: {artifact_name!r}")
            artifact_path = task_root / artifact_name
            artifact_path.write_text(content, encoding="utf-8", errors="ignore")
            extra_artifact_paths[artifact_name] = artifact_name

        grading_artifacts: dict[str, str] = {}
        if submission_step.exists() and submission_step.stat().st_size > 0:
            evaluation = evaluate_step_submission(
                task.task_id,
                submission_step,
                provenance_dir=grading_dir,
            )
            _write_json(grading_dir / "raw_metrics.json", evaluation.raw)
            _write_json(grading_dir / "normalized_metrics.json", evaluation.metrics)
            metrics = {
                "submission_exists": 1.0,
                "build_success": float(evaluation.metrics["build_success"]),
                "reward": float(evaluation.metrics["reward"]),
                "overall_score": float(
                    evaluation.metrics.get(
                        "overall_score", evaluation.metrics["task_score"]
                    )
                ),
                "task_score": float(evaluation.metrics["task_score"]),
                "score_error": str(evaluation.metrics.get("error_message", "")),
            }
            score_error = metrics.get("score_error") or None
        else:
            metrics = {
                "submission_exists": 0.0,
                "build_success": 0.0,
                "reward": 0.0,
                "overall_score": 0.0,
                "task_score": 0.0,
                "score_error": export_error or "missing_submission",
            }
            score_error = export_error or "missing_submission"
        _write_json(task_root / "metrics.json", metrics)
        for artifact_path in sorted(grading_dir.rglob("*")):
            if not artifact_path.is_file():
                continue
            relative_path = artifact_path.relative_to(task_root).as_posix()
            grading_artifacts[artifact_path.name] = relative_path

        remote_task_prefix = f"tasks/{task.task_id}"
        container_export_status: dict[str, Any] | None = None
        container_after_url = None
        if task_container_export_path is not None:
            container_export_status_path = _container_export_status_path(
                task_container_export_path
            )
            if container_export_status_path.exists():
                loaded_status = json.loads(
                    container_export_status_path.read_text(encoding="utf-8")
                )
                if isinstance(loaded_status, dict):
                    container_export_status = loaded_status
            if task_container_export_path.exists():
                final_container_export_path = task_root / "container_image_after.tar"
                task_container_export_path.replace(final_container_export_path)
                task_container_export_path = final_container_export_path
                container_after_url = storage.file_url(
                    f"{remote_task_prefix}/container_image_after.tar"
                )
            provenance_error = _container_provenance_error(
                container_export_status, container_after_url
            )
        else:
            container_export_status_path = None
            provenance_error = ""
        row_error = _join_errors(run_result.error, provenance_error)
        row = {
            "task_id": task.task_id,
            "difficulty": task.difficulty,
            "index": idx,
            "elapsed_s": elapsed,
            "attempt_count": attempt_count,
            "retry_errors": retry_errors,
            "metrics": metrics,
            "error": row_error,
            "export_error": export_error or None,
            "score_error": score_error,
            "provenance_error": provenance_error or None,
            "export": export_result,
            "container_export": _summarize_container_export_status(
                container_export_status
            ),
            "returncode": run_result.returncode,
            "session_id": run_result.session_id,
            "usage": usage_with_cost(
                spec.provider, spec.model, run_result.usage, access=spec.access
            ),
            "artifacts": {},
        }
        if container_after_url is not None:
            row["artifacts"]["container_image_after"] = container_after_url
        if code:
            row["artifacts"]["code_path"] = "code.py"
        if isinstance(export_result.get("process"), dict):
            row["artifacts"]["export_process_path"] = "export_process.json"
        if (
            container_export_status_path is not None
            and container_export_status_path.exists()
        ):
            row["artifacts"]["container_export_status_path"] = (
                container_export_status_path.relative_to(task_root).as_posix()
            )
        row["artifacts"].update(extra_artifact_paths)
        row["artifacts"].update(grading_artifacts)
        rows.append(row)
        prompt_signature_payload = json.dumps(
            sorted(prompt_manifest, key=lambda item: item["task_id"]),
            separators=(",", ":"),
        )
        report["benchmark_task_count"] = len(tasks)
        report["prompt_manifest"] = list(
            sorted(prompt_manifest, key=lambda item: item["task_id"])
        )
        report["prompt_signature"] = hashlib.sha256(
            prompt_signature_payload.encode("utf-8")
        ).hexdigest()
        report["summary"] = summarize_harness(rows)
        report["rows"] = rows
        report_path = out_root / "report.json"
        _write_json(report_path, report)
        task_bundle_url = storage.tree_url(remote_task_prefix)
        row["artifacts"]["task_bundle"] = task_bundle_url
        _write_json(report_path, report)

        print(
            f"[{spec.harness_id}] {idx}/{len(tasks)} {task.task_id}: "
            f"reward={metrics['reward']:.3f} submission={metrics['submission_exists']:.1f} "
            f"build={metrics['build_success']:.1f} overall={metrics['overall_score']:.3f}"
            + (f" error={row_error}" if row_error else "")
            + (f" score_error={score_error}" if score_error else ""),
            flush=True,
        )

    storage.upload_directory(
        out_root,
        "",
        f"Upload harness eval bundle for {spec.harness_id}",
    )
    print(f"Saved provenance to HF: {storage.tree_url('')}")
    print(f"Local run dir: {out_root}")


__all__ = [
    "BindMount",
    "CONTAINER_APP_DIR",
    "CONTAINER_DOCS_DIR",
    "CONTAINER_HOME_STEP_PATH",
    "CONTAINER_SUBMISSION_DIR",
    "CONTAINER_WORKDIR",
    "TASK_DEFS",
    "TASK_IDS",
    "TASK_MODULES",
    "build_harness_prompt",
    "build_step_prompt",
    "builtin_harness_name",
    "builtin_harness_ref",
    "compose_step_submission_prompt",
    "default_container_image",
    "docker_run_command",
    "docs_cli",
    "ensure_docker_image_available",
    "evaluate_step_submission",
    "eval_harness_main",
    "export_task_canaries_main",
    "export_task_media_main",
    "extract_code",
    "generate_build123d_docs_bundle",
    "load_harness_spec",
    "load_benchmark",
    "render_candidate_views",
    "render_views_main",
    "run_docker_command",
    "run_harness",
    "score_code_submission",
    "score_step_submission",
    "select_task_specs",
    "upload_public_tasks_main",
]
