from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from hashlib import sha256
from dataclasses import dataclass
from pathlib import Path
import fcntl
from urllib.parse import quote

from huggingface_hub import HfApi

from bench.config import env_value, required_env_value

HF_DATASET_BASE_URL = "https://huggingface.co/datasets"


def resolve_hf_token(repo_root: Path | None = None) -> str | None:
    value = env_value("HF_TOKEN", root=repo_root)
    return value or None


@dataclass(frozen=True)
class HFProvenanceConfig:
    repo_id: str


@dataclass
class HFProvenanceStore:
    repo_id: str
    path_prefix: str
    private: bool = False
    token: str | None = None

    def __post_init__(self) -> None:
        self.api = HfApi(token=self.token or resolve_hf_token())
        self.path_prefix = self.path_prefix.strip("/")
        self.api.create_repo(
            repo_id=self.repo_id,
            repo_type="dataset",
            private=self.private,
            exist_ok=True,
        )

    def repo_path(self, relative_path: str) -> str:
        relative = relative_path.strip("/")
        if not self.path_prefix:
            return relative
        return f"{self.path_prefix}/{relative}" if relative else self.path_prefix

    def file_url(self, relative_path: str) -> str:
        path_in_repo = quote(self.repo_path(relative_path), safe="/")
        return f"{HF_DATASET_BASE_URL}/{self.repo_id}/blob/main/{path_in_repo}"

    def tree_url(self, relative_path: str = "") -> str:
        path_in_repo = quote(self.repo_path(relative_path), safe="/")
        return f"{HF_DATASET_BASE_URL}/{self.repo_id}/tree/main/{path_in_repo}"

    def upload_file(self, local_path: Path, relative_path: str, commit_message: str) -> str:
        path_in_repo = self.repo_path(relative_path)
        self.api.upload_file(
            path_or_fileobj=str(local_path),
            path_in_repo=path_in_repo,
            repo_id=self.repo_id,
            repo_type="dataset",
            commit_message=commit_message,
        )
        return self.file_url(relative_path)

    def upload_directory(
        self, local_dir: Path, relative_path: str, commit_message: str
    ) -> str:
        del commit_message
        path_in_repo = self.repo_path(relative_path)
        worker_count = min(8, os.cpu_count() or 4)
        stage_root = Path(
            tempfile.mkdtemp(prefix=f".hf-large-upload-{local_dir.name}-", dir=local_dir.parent)
        )
        try:
            stage_dir = stage_root / path_in_repo
            _hardlink_tree(local_dir, stage_dir)
            self.api.upload_large_folder(
                repo_id=self.repo_id,
                repo_type="dataset",
                folder_path=stage_root,
                num_workers=worker_count,
                print_report=False,
            )
        finally:
            shutil.rmtree(stage_root, ignore_errors=True)
        return self.tree_url(relative_path)


def _hardlink_tree(source_dir: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    for source_path in sorted(source_dir.rglob("*")):
        relative = source_path.relative_to(source_dir)
        dest_path = dest_dir / relative
        if source_path.is_symlink():
            # Runtime workdirs contain transient virtualenv symlinks that can be broken
            # or can point at directories. They are not needed for provenance replay.
            continue
        if source_path.is_dir():
            dest_path.mkdir(parents=True, exist_ok=True)
            continue
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        if dest_path.exists():
            dest_path.unlink()
        try:
            os.link(source_path, dest_path)
        except OSError:
            shutil.copy2(source_path, dest_path)


def load_hf_provenance_config(repo_root: Path) -> HFProvenanceConfig:
    return HFProvenanceConfig(
        repo_id=required_env_value("HF_PROVENANCE_REPO_ID", root=repo_root),
    )


def _capture(cmd: list[str], cwd: Path | None = None) -> dict[str, object]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "command": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def _shared_git_root(cwd: Path) -> Path:
    proc = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return cwd
    git_common_dir = proc.stdout.strip()
    if not git_common_dir:
        return cwd
    return Path(git_common_dir).resolve().parent


def _link_or_copy(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    try:
        os.link(source, dest)
    except OSError:
        shutil.copy2(source, dest)


def _should_skip_snapshot(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    parts = relative.parts
    if not parts:
        return False
    if path.name == ".env" or path.name.startswith(".env."):
        return True
    blocked_names = {
        ".git",
        ".tmp",
        ".venv",
        ".pytest_cache",
        ".mypy_cache",
        "__pycache__",
        ".ruff_cache",
        ".cache",
        ".idea",
        ".vscode",
        "dist",
        "logs",
        "outputs",
    }
    return any(part in blocked_names for part in parts)


def _write_repo_snapshot(out_dir: Path, cwd: Path) -> str:
    archive_name = "repo_snapshot.tar.gz"
    archive_path = out_dir / archive_name
    with tarfile.open(archive_path, "w:gz") as tar:
        for path in sorted(cwd.rglob("*")):
            if _should_skip_snapshot(path, cwd):
                continue
            arcname = Path("repo_snapshot") / path.relative_to(cwd)
            tar.add(path, arcname=str(arcname), recursive=False)
    return archive_name


def write_provenance_bundle(out_dir: Path, cwd: Path) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)

    system_meta = {
        "python_version": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "uname": platform.uname()._asdict(),
        "cwd": str(cwd),
        "repo_env": {
            "OPENAI_API_KEY": bool(env_value("OPENAI_API_KEY")),
            "EXA_API_KEY": bool(env_value("EXA_API_KEY")),
            "HF_TOKEN": bool(env_value("HF_TOKEN")),
            "HF_PROVENANCE_REPO_ID": env_value("HF_PROVENANCE_REPO_ID"),
            "CAD_BENCH_AGENT_IMAGE": env_value("CAD_BENCH_AGENT_IMAGE"),
            "CODEX_AUTH_JSON_B64": bool(env_value("CODEX_AUTH_JSON_B64")),
        },
    }
    (out_dir / "system_info.json").write_text(
        json.dumps(system_meta, indent=2) + "\n", encoding="utf-8"
    )

    version_commands = {
        "python_version.txt": ["python", "--version"],
        "uv_version.txt": ["uv", "--version"],
    }
    for filename, binary in (
        ("codex_version.txt", "codex"),
        ("pi_version.txt", "pi"),
        ("blender_version.txt", "blender"),
    ):
        if shutil.which(binary):
            version_commands[filename] = [binary, "--version"]
    for filename, cmd in version_commands.items():
        result = _capture(cmd, cwd=cwd)
        body = result["stdout"] or result["stderr"] or ""
        (out_dir / filename).write_text(str(body), encoding="utf-8", errors="ignore")

    freeze = _capture(["uv", "pip", "freeze"], cwd=cwd)
    (out_dir / "uv_pip_freeze.txt").write_text(
        str(freeze["stdout"] or freeze["stderr"] or ""),
        encoding="utf-8",
        errors="ignore",
    )

    git_meta = {
        "rev_parse_head": _capture(["git", "rev-parse", "HEAD"], cwd=cwd),
        "branch": _capture(["git", "branch", "--show-current"], cwd=cwd),
        "status": _capture(["git", "status", "--short"], cwd=cwd),
        "tracked_files": _capture(["git", "ls-files"], cwd=cwd),
        "untracked_files": _capture(
            ["git", "ls-files", "--others", "--exclude-standard"], cwd=cwd
        ),
        "diff": _capture(
            [
                "git",
                "diff",
                "--binary",
                "--",
                ".",
                ":(exclude).env",
                ":(exclude).env.*",
            ],
            cwd=cwd,
        ),
        "diff_cached": _capture(
            [
                "git",
                "diff",
                "--cached",
                "--binary",
                "--",
                ".",
                ":(exclude).env",
                ":(exclude).env.*",
            ],
            cwd=cwd,
        ),
    }
    (out_dir / "git_metadata.json").write_text(
        json.dumps(git_meta, indent=2) + "\n", encoding="utf-8"
    )
    repo_snapshot = _write_repo_snapshot(out_dir, cwd)

    files = {
        "system_info": "system_info.json",
        "python_version": "python_version.txt",
        "uv_version": "uv_version.txt",
        "uv_pip_freeze": "uv_pip_freeze.txt",
        "git_metadata": "git_metadata.json",
        "repo_snapshot": repo_snapshot,
    }
    if "codex_version.txt" in version_commands:
        files["codex_version"] = "codex_version.txt"
    if "pi_version.txt" in version_commands:
        files["pi_version"] = "pi_version.txt"
    if "blender_version.txt" in version_commands:
        files["blender_version"] = "blender_version.txt"
    return {key: str((out_dir / value).name) for key, value in files.items()}


def write_docker_bundle(out_dir: Path, cwd: Path, image: str) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    inspect = _capture(["docker", "image", "inspect", image], cwd=cwd)
    inspect_path = out_dir / "docker_image_inspect.json"
    inspect_path.write_text(
        json.dumps(inspect, indent=2) + "\n", encoding="utf-8"
    )

    cache_root = _shared_git_root(cwd) / ".tmp" / "docker_image_cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    inspect_bytes = json.dumps(inspect, sort_keys=True).encode("utf-8")
    cache_key = sha256(inspect_bytes).hexdigest()
    cache_tar = cache_root / f"{cache_key}.tar"
    lock_path = cache_root / f"{cache_key}.lock"
    image_tar = out_dir / "docker_image_before.tar"
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        if not cache_tar.exists():
            tmp_tar = cache_tar.with_name(cache_tar.name + ".tmp")
            tmp_tar.unlink(missing_ok=True)
            subprocess.run(
                ["docker", "save", "--output", str(tmp_tar), image],
                cwd=cwd,
                check=True,
            )
            tmp_tar.replace(cache_tar)
        _link_or_copy(cache_tar, image_tar)
    return {
        "docker_image_tar": image_tar.name,
        "docker_image_inspect": "docker_image_inspect.json",
    }
