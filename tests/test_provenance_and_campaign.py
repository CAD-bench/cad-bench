import importlib.util
import os
import json
from pathlib import Path
import subprocess
import sys
from bench.provenance import (
    HFProvenanceStore,
    _hardlink_tree,
    _should_skip_snapshot,
    load_hf_provenance_config,
    resolve_hf_token,
    write_docker_bundle,
)
from bench.tasks_repo import build_public_task_manifest, write_task_canaries


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_select_cohort_keeps_different_benchmark_signatures_separate() -> None:
    mod = _load_module(
        Path(__file__).resolve().parent.parent / "scripts" / "render_site.py",
        "render_site",
    )
    mod.FULL_BENCHMARK_TASK_COUNT = 1
    rows = [
        {
            "task_id": "cube_20mm_z_minus",
            "error": "",
            "metrics": {
                "submission_exists": 1.0,
                "build_success": 1.0,
                "reward": 1.0,
                "overall_score": 1.0,
                "task_score": 1.0,
            },
        }
    ]
    item_a = mod.ImportedReport(
        report={
            "harness": {"id": "openai/gpt-5.4-web-med", "provider": "openai"},
            "rows": rows,
            "timestamp_utc": "2026-04-12T00:00:00+00:00",
        },
        run_name="run_a",
        row_count=1,
        git_head="abc123",
        benchmark_signature="prompt-a",
        tasks_signature="tasks",
        source_path="a",
    )
    item_b = mod.ImportedReport(
        report={
            "harness": {"id": "openai/gpt-5.4-web-high", "provider": "openai"},
            "rows": rows,
            "timestamp_utc": "2026-04-12T00:00:01+00:00",
        },
        run_name="run_b",
        row_count=1,
        git_head="abc123",
        benchmark_signature="prompt-b",
        tasks_signature="tasks",
        source_path="b",
    )

    _, cohort = mod._select_cohort([item_a, item_b])

    assert len(cohort) == 1


def test_import_payload_uses_correct_cohort_tuple_indexes() -> None:
    mod = _load_module(
        Path(__file__).resolve().parent.parent / "scripts" / "render_site.py",
        "render_site",
    )
    mod.FULL_BENCHMARK_TASK_COUNT = 7
    item = mod.ImportedReport(
        report={
            "harness": {"id": "openai/gpt-5.4-web-med", "provider": "openai"},
            "rows": [
                {
                    "task_id": "cube_20mm_z_minus",
                    "error": "",
                    "metrics": {
                        "submission_exists": 1.0,
                        "build_success": 1.0,
                        "reward": 1.0,
                        "overall_score": 1.0,
                        "task_score": 1.0,
                    },
                }
            ],
            "timestamp_utc": "2026-04-12T00:00:00+00:00",
        },
        run_name="run_a",
        row_count=7,
        git_head="git123",
        benchmark_signature="bench123",
        tasks_signature="tasks123",
        source_path="a",
    )

    cohort_key, _ = mod._select_cohort([item])

    assert cohort_key == ("tasks123", "git123", "bench123", 7)
    assert cohort_key[3] == 7
    assert cohort_key[2] == "bench123"


def test_select_publishable_reports_keeps_multiple_publishable_cohorts() -> None:
    mod = _load_module(
        Path(__file__).resolve().parent.parent / "scripts" / "render_site.py",
        "render_site_publishable",
    )
    mod.FULL_BENCHMARK_TASK_COUNT = 1
    rows = [
        {
            "task_id": "cube_20mm_z_minus",
            "error": "",
            "metrics": {
                "submission_exists": 1.0,
                "build_success": 1.0,
                "reward": 1.0,
                "overall_score": 1.0,
                "task_score": 1.0,
            },
        }
    ]
    item_a = mod.ImportedReport(
        report={
            "harness": {"id": "pi/gpt-5.4-web-low", "provider": "pi"},
            "rows": rows,
            "timestamp_utc": "2026-04-12T00:00:00+00:00",
        },
        run_name="run_a",
        row_count=1,
        git_head="git-a",
        benchmark_signature="bench-a",
        tasks_signature="tasks-a",
        source_path="a",
    )
    item_b = mod.ImportedReport(
        report={
            "harness": {"id": "pi/gpt-5.4-offline-low", "provider": "pi"},
            "rows": rows,
            "timestamp_utc": "2026-04-12T00:00:01+00:00",
        },
        run_name="run_b",
        row_count=1,
        git_head="git-b",
        benchmark_signature="bench-b",
        tasks_signature="tasks-b",
        source_path="b",
    )

    selected = mod._select_publishable_reports([item_b, item_a])

    assert [item.run_name for item in selected] == ["run_b", "run_a"]


def test_run_clean_matrix_runs_selected_harnesses_once(monkeypatch, tmp_path: Path) -> None:
    mod = _load_module(
        Path(__file__).resolve().parent.parent / "scripts" / "run_clean_matrix.py",
        "run_clean_matrix",
    )
    runner = tmp_path / "eval-harness"
    runner.write_text("", encoding="utf-8")
    calls: list[list[str]] = []

    monkeypatch.setattr(mod, "RUNNER", runner)
    monkeypatch.setattr(
        mod,
        "all_harness_refs",
        lambda: [
            mod.HarnessRef("openai", "gpt-5.4", "offline", "high"),
            mod.HarnessRef("openai", "gpt-5.4-mini", "offline", "high"),
        ],
    )
    monkeypatch.setattr(mod.bench, "TASK_IDS", ["t1", "t2"])
    monkeypatch.setattr(mod.bench, "select_task_specs", lambda task_ids: task_ids)
    monkeypatch.setattr(mod, "env_value", lambda name: "test-key")

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_clean_matrix.py", "--providers", "openai", "--task-ids", "t1,t2"],
    )

    assert mod.main() == 0
    assert len(calls) == 2
    assert all(call[1] == "--harness" for call in calls)
    assert all(call[-1] == "t1,t2" for call in calls)


def test_hf_provenance_config_reads_only_dot_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("HF_PROVENANCE_REPO_ID", raising=False)
    (tmp_path / ".env").write_text(
        "HF_PROVENANCE_REPO_ID=repo/from-env\n",
        encoding="utf-8",
    )

    cfg = load_hf_provenance_config(tmp_path)

    assert cfg.repo_id == "repo/from-env"


def test_resolve_hf_token_reads_only_dot_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGINGFACE_HUB_TOKEN", raising=False)
    monkeypatch.delenv("HF_ACCESS_TOKEN", raising=False)
    (tmp_path / ".env").write_text("HF_TOKEN=hf_from_env_file\n", encoding="utf-8")

    assert resolve_hf_token(tmp_path) == "hf_from_env_file"


def test_repo_snapshot_skips_dot_env_files(tmp_path: Path) -> None:
    root = tmp_path
    env_path = root / ".env"
    nested_env_path = root / "configs" / ".env.local"
    normal_path = root / "README.md"
    nested_env_path.parent.mkdir()
    env_path.write_text("secret\n", encoding="utf-8")
    nested_env_path.write_text("secret\n", encoding="utf-8")
    normal_path.write_text("public\n", encoding="utf-8")

    assert _should_skip_snapshot(env_path, root) is True
    assert _should_skip_snapshot(nested_env_path, root) is True
    assert _should_skip_snapshot(normal_path, root) is False


def test_repo_snapshot_skips_tmp_runtime_homes(tmp_path: Path) -> None:
    root = tmp_path
    runtime_auth = root / ".tmp" / "runtime_homes" / "cad_bench_codex_x" / ".codex" / "auth.json"
    runtime_auth.parent.mkdir(parents=True)
    runtime_auth.write_text("secret\n", encoding="utf-8")

    assert _should_skip_snapshot(runtime_auth, root) is True


def test_hf_provenance_store_defaults_public() -> None:
    assert HFProvenanceStore.__dataclass_fields__["private"].default is False


def test_hardlink_tree_and_upload_directory_use_large_folder(
    tmp_path: Path, monkeypatch
) -> None:
    source_dir = tmp_path / "source"
    nested_file = source_dir / "nested" / "artifact.txt"
    nested_file.parent.mkdir(parents=True)
    nested_file.write_text("artifact\n", encoding="utf-8")

    upload_calls: list[dict[str, object]] = []

    class FakeApi:
        def create_repo(self, **_kwargs) -> None:
            return None

        def upload_large_folder(self, **kwargs) -> None:
            upload_calls.append(kwargs)
            stage_root = Path(str(kwargs["folder_path"]))
            staged = stage_root / "evals" / "harnesses" / "sample-run" / "nested" / "artifact.txt"
            assert staged.exists()
            assert staged.read_text(encoding="utf-8") == "artifact\n"
            assert staged.stat().st_ino == nested_file.stat().st_ino

    monkeypatch.setattr("bench.provenance.HfApi", lambda token=None: FakeApi())
    monkeypatch.setattr("bench.provenance.resolve_hf_token", lambda repo_root=None: "hf_test")

    store = HFProvenanceStore(
        repo_id="owner/cad-bench-provenance",
        path_prefix="evals/harnesses/sample-run",
        token="hf_test",
    )

    url = store.upload_directory(source_dir, "", "ignored commit message")

    assert url.endswith("/evals/harnesses/sample-run")
    assert len(upload_calls) == 1
    assert upload_calls[0]["repo_type"] == "dataset"


def test_hardlink_tree_recreates_directory_structure(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_file = source_dir / "a" / "b" / "payload.json"
    source_file.parent.mkdir(parents=True)
    source_file.write_text('{"ok": true}\n', encoding="utf-8")

    _hardlink_tree(source_dir, target_dir)

    linked = target_dir / "a" / "b" / "payload.json"
    assert linked.exists()
    assert linked.read_text(encoding="utf-8") == '{"ok": true}\n'
    assert linked.stat().st_ino == source_file.stat().st_ino


def test_hardlink_tree_falls_back_to_copy_when_link_forbidden(
    tmp_path: Path, monkeypatch
) -> None:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_file = source_dir / "a" / "b" / "payload.step"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("step-bytes\n", encoding="utf-8")

    real_link = os.link

    def fake_link(src, dst, *args, **kwargs):
        if str(src).endswith("payload.step"):
            raise PermissionError("operation not permitted")
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", fake_link)

    _hardlink_tree(source_dir, target_dir)

    copied = target_dir / "a" / "b" / "payload.step"
    assert copied.exists()
    assert copied.read_text(encoding="utf-8") == "step-bytes\n"
    assert copied.stat().st_ino != source_file.stat().st_ino


def test_hardlink_tree_skips_broken_symlinks(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    (source_dir / "payload.txt").write_text("ok\n", encoding="utf-8")
    os.symlink(source_dir / "missing.txt", source_dir / "broken-link")

    _hardlink_tree(source_dir, target_dir)

    assert (target_dir / "payload.txt").exists()
    assert not (target_dir / "broken-link").exists()


def test_hardlink_tree_skips_live_symlinks(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    nested_dir = source_dir / "nested"
    nested_dir.mkdir()
    target_file = nested_dir / "payload.txt"
    target_file.write_text("ok\n", encoding="utf-8")
    os.symlink(target_file, source_dir / "live-file-link")
    os.symlink(nested_dir, source_dir / "live-dir-link")

    _hardlink_tree(source_dir, target_dir)

    assert (target_dir / "nested" / "payload.txt").exists()
    assert not (target_dir / "live-file-link").exists()
    assert not (target_dir / "live-dir-link").exists()


def test_write_docker_bundle_reuses_cached_image_tar(
    tmp_path: Path, monkeypatch
) -> None:
    git_common = tmp_path / ".git"
    git_common.mkdir()
    shared_root = tmp_path
    run_calls: list[list[str]] = []

    def fake_run(cmd, cwd=None, capture_output=False, text=False, check=False, **kwargs):
        if cmd[:3] == ["git", "rev-parse", "--path-format=absolute"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=str(git_common) + "\n", stderr="")
        run_calls.append(cmd)
        if cmd[:3] == ["docker", "save", "--output"]:
            Path(cmd[3]).write_text("docker image bytes", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr("bench.provenance.subprocess.run", fake_run)
    monkeypatch.setattr(
        "bench.provenance._capture",
        lambda cmd, cwd=None: {"command": cmd, "returncode": 0, "stdout": '[{"Id":"sha256:test"}]', "stderr": ""},
    )

    first_out = tmp_path / "run1" / "metadata" / "docker"
    second_out = tmp_path / "run2" / "metadata" / "docker"

    files1 = write_docker_bundle(first_out, shared_root, "cad:test")
    files2 = write_docker_bundle(second_out, shared_root, "cad:test")

    assert files1["docker_image_tar"] == "docker_image_before.tar"
    assert files2["docker_image_tar"] == "docker_image_before.tar"
    assert len([cmd for cmd in run_calls if cmd[:3] == ["docker", "save", "--output"]]) == 1
    first_tar = first_out / "docker_image_before.tar"
    second_tar = second_out / "docker_image_before.tar"
    assert first_tar.read_text(encoding="utf-8") == "docker image bytes"
    assert second_tar.read_text(encoding="utf-8") == "docker image bytes"
    assert os.stat(first_tar).st_ino == os.stat(second_tar).st_ino


def test_build_public_task_manifest_records_file_hashes(tmp_path: Path) -> None:
    task_dir = tmp_path / "sample_task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text(
        'task_id = "sample_task"\ndifficulty = "easy"\norder = 1\n[expected]\nside = 1\n',
        encoding="utf-8",
    )
    (task_dir / "prompt.txt").write_text("prompt\n", encoding="utf-8")
    manifest = build_public_task_manifest(tmp_path)

    assert len(manifest) == 1
    assert manifest[0]["task_id"] == "sample_task"
    assert "bundle_sha256" in manifest[0]
    assert "task.toml" in manifest[0]["files"]
    assert "prompt.txt" in manifest[0]["files"]


def test_write_task_canaries_supports_optional_salt(tmp_path: Path) -> None:
    task_dir = tmp_path / "sample_task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text(
        'task_id = "sample_task"\ndifficulty = "easy"\norder = 1\n[expected]\nside = 1\n',
        encoding="utf-8",
    )
    out_path = tmp_path / "canaries.json"

    write_task_canaries(task_dirs=[task_dir], out_path=out_path, salt="pepper")
    payload = json.loads(out_path.read_text(encoding="utf-8"))

    assert payload["salted"] is True
    assert payload["records"][0]["task_id"] == "sample_task"
    assert payload["records"][0]["salted_bundle_sha256"]


def test_collect_approved_reports_loads_local_report(monkeypatch, tmp_path: Path) -> None:
    mod = _load_module(
        Path(__file__).resolve().parent.parent / "scripts" / "render_site.py",
        "render_site_local_manifest",
    )
    report_path = tmp_path / "sample" / "report.json"
    report_path.parent.mkdir()
    report_path.write_text(
        json.dumps(
            {
                "timestamp_utc": "2026-04-12T00:00:00+00:00",
                "harness": {"id": "openai/gpt-5.4-web-med", "provider": "openai"},
                "tasks": ["cube_20mm_z_minus"],
                "rows": [{"task_id": "cube_20mm_z_minus", "error": ""}],
            }
        ),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "approved_runs.json"
    manifest_path.write_text(
        json.dumps([{"kind": "local_report", "path": str(report_path.relative_to(tmp_path))}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "APPROVED_RUNS_JSON", manifest_path)

    reports = mod._collect_approved_reports()

    assert len(reports) == 1
    assert reports[0].run_name == "sample"
    assert reports[0].source_path == str(report_path)


def test_collect_approved_reports_loads_hf_report(monkeypatch, tmp_path: Path) -> None:
    mod = _load_module(
        Path(__file__).resolve().parent.parent / "scripts" / "render_site.py",
        "render_site_hf_manifest",
    )
    manifest_path = tmp_path / "approved_runs.json"
    manifest_path.write_text(
        json.dumps(
            [
                {
                    "kind": "hf_report",
                    "repo_id": "owner/cad-bench-provenance",
                    "revision": "abc123",
                    "path_in_repo": "evals/harnesses/sample/report.json",
                }
            ]
        ),
        encoding="utf-8",
    )
    imported = mod.ImportedReport(
        report={"rows": [], "tasks": []},
        run_name="sample",
        row_count=0,
        git_head="deadbeef",
        benchmark_signature="bench",
        tasks_signature="tasks",
        source_path="hf://owner/cad-bench-provenance@abc123/evals/harnesses/sample/report.json",
    )
    monkeypatch.setattr(mod, "APPROVED_RUNS_JSON", manifest_path)
    monkeypatch.setattr(mod, "_collect_hf_report", lambda **_: imported)

    reports = mod._collect_approved_reports()

    assert reports == [imported]


def test_approved_entry_for_run_accepts_missing_submission_rows(
    tmp_path: Path, monkeypatch
) -> None:
    mod = _load_module(
        Path(__file__).resolve().parent.parent / "scripts" / "run_publish_matrix.py",
        "run_publish_matrix_gate_reject",
    )
    mod.FULL_BENCHMARK_TASK_COUNT = 2
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(
        json.dumps(
            {
                "benchmark_task_count": 2,
                "rows": [
                    {
                        "task_id": "cube_20mm_z_minus",
                        "metrics": {
                            "submission_exists": 1.0,
                            "build_success": 1.0,
                        },
                    },
                    {
                        "task_id": "hex_nut_af17_h8_bore10",
                        "metrics": {
                            "submission_exists": 0.0,
                            "build_success": 0.0,
                            "score_error": "missing_submission",
                        },
                    },
                ],
                "storage": {
                    "repo_id": "anonymous/cad-bench-provenance",
                    "run_prefix": "evals/harnesses/example",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "resolve_hf_token", lambda _: "test-token")

    class FakeInfo:
        sha = "abc123"

    class FakeApi:
        def __init__(self, *args, **kwargs):
            pass

        def dataset_info(self, *, repo_id, token):
            assert repo_id == "anonymous/cad-bench-provenance"
            assert token == "test-token"
            return FakeInfo()

    monkeypatch.setattr(mod, "HfApi", FakeApi)

    entry = mod._approved_entry_for_run(run_dir)

    assert entry.repo_id == "anonymous/cad-bench-provenance"
    assert entry.revision == "abc123"
    assert entry.path_in_repo == "evals/harnesses/example/report.json"


def test_approved_entry_for_run_rejects_partial_benchmark(
    tmp_path: Path, monkeypatch
) -> None:
    import pytest

    mod = _load_module(
        Path(__file__).resolve().parent.parent / "scripts" / "run_publish_matrix.py",
        "run_publish_matrix_gate_partial",
    )
    mod.FULL_BENCHMARK_TASK_COUNT = 2
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(
        json.dumps(
            {
                "benchmark_task_count": 1,
                "rows": [
                    {
                        "task_id": "cube_20mm_z_minus",
                        "metrics": {
                            "submission_exists": 1.0,
                            "build_success": 1.0,
                        },
                    },
                ],
                "storage": {
                    "repo_id": "anonymous/cad-bench-provenance",
                    "run_prefix": "evals/harnesses/partial",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "resolve_hf_token", lambda _: "test-token")

    with pytest.raises(ValueError, match="partial benchmark cannot be published"):
        mod._approved_entry_for_run(run_dir)


def test_approved_entry_for_run_accepts_complete_clean_report(
    tmp_path: Path, monkeypatch
) -> None:
    mod = _load_module(
        Path(__file__).resolve().parent.parent / "scripts" / "run_publish_matrix.py",
        "run_publish_matrix_gate_accept",
    )
    mod.FULL_BENCHMARK_TASK_COUNT = 2
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(
        json.dumps(
            {
                "benchmark_task_count": 2,
                "rows": [
                    {
                        "task_id": "cube_20mm_z_minus",
                        "metrics": {
                            "submission_exists": 1.0,
                            "build_success": 1.0,
                        },
                    },
                    {
                        "task_id": "hex_nut_af17_h8_bore10",
                        "metrics": {
                            "submission_exists": 1.0,
                            "build_success": 1.0,
                        },
                    },
                ],
                "storage": {
                    "repo_id": "anonymous/cad-bench-provenance",
                    "run_prefix": "evals/harnesses/example",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "resolve_hf_token", lambda _: "test-token")

    class FakeInfo:
        sha = "abc123"

    class FakeApi:
        def __init__(self, *args, **kwargs):
            pass

        def dataset_info(self, *, repo_id, token):
            assert repo_id == "anonymous/cad-bench-provenance"
            assert token == "test-token"
            return FakeInfo()

    monkeypatch.setattr(mod, "HfApi", FakeApi)

    entry = mod._approved_entry_for_run(run_dir)

    assert entry.repo_id == "anonymous/cad-bench-provenance"
    assert entry.revision == "abc123"
    assert entry.path_in_repo == "evals/harnesses/example/report.json"


def test_approved_entry_for_run_allows_build_failure_with_submission(
    tmp_path: Path, monkeypatch
) -> None:
    mod = _load_module(
        Path(__file__).resolve().parent.parent / "scripts" / "run_publish_matrix.py",
        "run_publish_matrix_gate_build_failure",
    )
    mod.FULL_BENCHMARK_TASK_COUNT = 2
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(
        json.dumps(
            {
                "benchmark_task_count": 2,
                "rows": [
                    {
                        "task_id": "cube_20mm_z_minus",
                        "metrics": {
                            "submission_exists": 1.0,
                            "build_success": 1.0,
                        },
                    },
                    {
                        "task_id": "m3x6_socket_head_zminus",
                        "metrics": {
                            "submission_exists": 1.0,
                            "build_success": 0.0,
                            "score_error": "'NoneType' object has no attribute 'NbNodes'",
                        },
                    },
                ],
                "storage": {
                    "repo_id": "anonymous/cad-bench-provenance",
                    "run_prefix": "evals/harnesses/example-build-failure",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "resolve_hf_token", lambda _: "test-token")

    class FakeInfo:
        sha = "abc123"

    class FakeApi:
        def __init__(self, *args, **kwargs):
            pass

        def dataset_info(self, *, repo_id, token):
            assert repo_id == "anonymous/cad-bench-provenance"
            assert token == "test-token"
            return FakeInfo()

    monkeypatch.setattr(mod, "HfApi", FakeApi)

    entry = mod._approved_entry_for_run(run_dir)

    assert entry.path_in_repo == "evals/harnesses/example-build-failure/report.json"
