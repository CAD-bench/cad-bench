from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

from bench.config import env_value
from bench.provenance import resolve_hf_token

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCAL_TASKS_ROOT = REPO_ROOT / "tasks"
TASKS_DATASET_DEFAULT_NAME = "cad-bench-tasks"
TASKS_MANIFEST_PATH = "tasks_manifest.json"
TASKS_SHARED_FILES = ("blender_common.py",)
TASK_UPLOAD_IGNORE_PATTERNS = ("__pycache__", "*.pyc")


@dataclass(frozen=True)
class HFTasksConfig:
    repo_id: str
    revision: str


def _tasks_repo_id_from_env() -> str:
    explicit = env_value("HF_TASKS_REPO_ID")
    if explicit:
        return explicit
    provenance_repo = env_value("HF_PROVENANCE_REPO_ID")
    if "/" in provenance_repo:
        owner = provenance_repo.split("/", 1)[0].strip()
        if owner:
            return f"{owner}/{TASKS_DATASET_DEFAULT_NAME}"
    return ""


def load_hf_tasks_config() -> HFTasksConfig | None:
    repo_id = _tasks_repo_id_from_env()
    if not repo_id:
        return None
    revision = env_value("HF_TASKS_REVISION", default="main") or "main"
    return HFTasksConfig(repo_id=repo_id, revision=revision)


def _looks_like_local_tasks_root(root: Path) -> bool:
    return root.exists() and any(root.glob("*/task.toml"))


def _iter_task_dirs(root: Path) -> list[Path]:
    return [path.parent for path in sorted(root.glob("*/task.toml"))]


@lru_cache(maxsize=1)
def tasks_root() -> Path:
    if _looks_like_local_tasks_root(LOCAL_TASKS_ROOT):
        return LOCAL_TASKS_ROOT
    cfg = load_hf_tasks_config()
    if cfg is None:
        raise RuntimeError(
            "task definitions are not available locally and HF_TASKS_REPO_ID is unset"
        )
    snapshot_path = Path(
        snapshot_download(
            repo_id=cfg.repo_id,
            repo_type="dataset",
            revision=cfg.revision,
            token=resolve_hf_token(REPO_ROOT),
            allow_patterns=["tasks/*", TASKS_MANIFEST_PATH],
        )
    )
    root = snapshot_path / "tasks"
    if not _looks_like_local_tasks_root(root):
        raise RuntimeError(f"HF tasks snapshot is missing task files: {root}")
    return root


@lru_cache(maxsize=1)
def task_manifest() -> list[dict[str, object]]:
    root = tasks_root()
    manifest_path = root.parent / TASKS_MANIFEST_PATH
    if manifest_path.exists():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
    records: list[dict[str, object]] = []
    for task_root in _iter_task_dirs(root):
        records.append({"task_id": task_root.name})
    return records


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _task_manifest_record(task_root: Path) -> dict[str, object]:
    files = {}
    for path in sorted(task_root.rglob("*")):
        if any(part == "__pycache__" for part in path.parts) or path.suffix == ".pyc":
            continue
        if path.is_file():
            files[str(path.relative_to(task_root))] = _sha256_path(path)
    bundle_hash = _sha256_bytes(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return {
        "task_id": task_root.name,
        "bundle_sha256": bundle_hash,
        "files": files,
    }


def build_public_task_manifest(root: Path) -> list[dict[str, object]]:
    return [_task_manifest_record(task_root) for task_root in _iter_task_dirs(root)]


def upload_public_tasks(
    *,
    repo_id: str,
    revision: str = "main",
    token: str | None = None,
    tasks_root_dir: Path | None = None,
) -> str:
    api = HfApi(token=token or resolve_hf_token(REPO_ROOT))
    api.create_repo(repo_id=repo_id, repo_type="dataset", private=False, exist_ok=True)
    root = (tasks_root_dir or LOCAL_TASKS_ROOT).resolve()
    if not _looks_like_local_tasks_root(root):
        raise RuntimeError(f"local task definitions are missing under {root}")
    manifest = build_public_task_manifest(root)
    manifest_path = REPO_ROOT / ".cache" / "tasks_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="cad-bench-public-tasks-") as temp_dir:
        staged_root = Path(temp_dir) / "tasks"
        staged_root.mkdir(parents=True, exist_ok=True)
        for task_root in _iter_task_dirs(root):
            shutil.copytree(
                task_root,
                staged_root / task_root.name,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(*TASK_UPLOAD_IGNORE_PATTERNS),
            )
        for shared_name in TASKS_SHARED_FILES:
            shared_path = root / shared_name
            if shared_path.is_file():
                shutil.copy2(shared_path, staged_root / shared_name)
        api.upload_folder(
            folder_path=str(staged_root),
            path_in_repo="tasks",
            repo_id=repo_id,
            repo_type="dataset",
            revision=revision,
            commit_message="Upload public CAD benchmark tasks",
        )
    api.upload_file(
        path_or_fileobj=str(manifest_path),
        path_in_repo=TASKS_MANIFEST_PATH,
        repo_id=repo_id,
        repo_type="dataset",
        revision=revision,
        commit_message="Upload public CAD benchmark task manifest",
    )
    return repo_id


def write_task_canaries(
    *,
    task_dirs: list[Path],
    out_path: Path,
    salt: str = "",
) -> Path:
    records = []
    salt_bytes = salt.encode("utf-8")
    for task_dir in sorted(task_dirs):
        if not (task_dir / "task.toml").exists():
            raise FileNotFoundError(f"missing task.toml in {task_dir}")
        record = _task_manifest_record(task_dir)
        record["salted_bundle_sha256"] = _sha256_bytes(
            salt_bytes + str(record["bundle_sha256"]).encode("utf-8")
        )
        records.append(record)
    payload = {
        "salted": bool(salt),
        "records": records,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return out_path


def upload_public_tasks_main() -> int:
    parser = argparse.ArgumentParser(
        description="Upload public task definitions to the configured HF dataset."
    )
    parser.add_argument(
        "--repo-id",
        default="",
        help="HF dataset repo id. Defaults to HF_TASKS_REPO_ID or a repo derived from HF_PROVENANCE_REPO_ID.",
    )
    parser.add_argument(
        "--tasks-root",
        default="",
        help="Local directory containing task subdirectories to upload. Defaults to ./tasks.",
    )
    args = parser.parse_args()
    cfg = load_hf_tasks_config()
    repo_id = args.repo_id.strip() or (cfg.repo_id if cfg is not None else "")
    if not repo_id:
        raise SystemExit("missing HF task repo id; set HF_TASKS_REPO_ID or pass --repo-id")
    upload_public_tasks(
        repo_id=repo_id,
        tasks_root_dir=Path(args.tasks_root).resolve() if args.tasks_root else None,
    )
    print(repo_id)
    return 0


def export_task_canaries_main() -> int:
    parser = argparse.ArgumentParser(
        description="Write hash canaries for a set of task directories."
    )
    parser.add_argument(
        "--task-dir",
        action="append",
        default=[],
        help="Task directory to hash. Defaults to all local task directories.",
    )
    parser.add_argument(
        "--tasks-root",
        default="",
        help="Directory containing task subdirectories when exporting all task canaries.",
    )
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument(
        "--salt-env",
        default="",
        help="Environment variable providing an optional salt for the canary hashes.",
    )
    args = parser.parse_args()
    root = (
        Path(args.tasks_root).resolve()
        if args.tasks_root
        else (
            LOCAL_TASKS_ROOT
            if _looks_like_local_tasks_root(LOCAL_TASKS_ROOT)
            else tasks_root()
        )
    )
    task_dirs = [Path(path).resolve() for path in args.task_dir] if args.task_dir else [
        path.parent.resolve() for path in sorted(root.glob("*/task.toml"))
    ]
    salt = env_value(args.salt_env) if args.salt_env else ""
    out_path = write_task_canaries(task_dirs=task_dirs, out_path=Path(args.out), salt=salt)
    print(out_path)
    return 0
