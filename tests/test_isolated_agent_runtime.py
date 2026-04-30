from contextlib import contextmanager
import json
from pathlib import Path
import subprocess

from bench import runner as bench
import pytest
from conftest import load_builtin_harness
from harnesses.pi import utils as pi_utils
from harnesses import utils as harness_utils


def test_default_container_image_uses_env(monkeypatch) -> None:
    monkeypatch.setattr(bench, "required_env_value", lambda name: "custom:image")
    assert bench.default_container_image() == "custom:image"


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("codex", True),
        ("pi", True),
        ("openai", False),
        ("openai_codex", False),
        ("deepseek", False),
    ],
)
def test_post_run_container_images_are_only_saved_for_agent_providers(
    provider: str, expected: bool
) -> None:
    spec = harness_utils.HarnessSpec(
        harness_id=f"{provider}/model-offline-none",
        provider=provider,
        strategy="one_shot_code",
        model="model",
        access="offline",
    )

    assert bench._save_post_run_container_image(spec) is expected


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("codex", True),
        ("pi", True),
        ("openai", False),
        ("openai_codex", False),
        ("deepseek", False),
    ],
)
def test_container_image_provenance_is_only_included_for_agent_providers(
    provider: str, expected: bool
) -> None:
    spec = harness_utils.HarnessSpec(
        harness_id=f"{provider}/model-offline-none",
        provider=provider,
        strategy="one_shot_code",
        model="model",
        access="offline",
    )

    assert bench._include_container_image_provenance(spec) is expected


def test_docker_run_command_builds_isolated_mounts(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    docs = tmp_path / "docs"
    workdir.mkdir()
    docs.mkdir()

    cmd = bench.docker_run_command(
        image="cad-build123d-bench:latest",
        entrypoint="codex",
        mounts=[
            bench.BindMount(workdir, bench.CONTAINER_WORKDIR),
            bench.BindMount(docs, bench.CONTAINER_DOCS_DIR, read_only=True),
        ],
        env={"OPENAI_API_KEY": "secret"},
        args=["exec", "--json", "prompt"],
    )

    assert cmd[:6] == ["docker", "run", "--rm", "--init", "--network", "bridge"]
    assert "--entrypoint" in cmd
    assert "codex" in cmd
    assert f"type=bind,src={workdir},dst={bench.CONTAINER_WORKDIR}" in cmd
    assert f"type=bind,src={docs},dst={bench.CONTAINER_DOCS_DIR},readonly" in cmd
    assert "cad-build123d-bench:latest" in cmd


def test_run_docker_command_saves_post_run_container_image(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    export_path = tmp_path / "container_after.tar"
    temp_export_path = export_path.with_name(export_path.name + ".tmp")

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["docker", "save"]:
            Path(cmd[3]).write_text("image", encoding="utf-8")
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="out", stderr=""
        )

    monkeypatch.setattr(bench.subprocess, "run", fake_run)
    result = bench.run_docker_command(
        image="cad-build123d-bench:test",
        entrypoint="uv",
        mounts=[],
        env={},
        args=["run", "echo", "ok"],
        container_export_path=export_path,
        container_name="cad-bench-test",
    )

    assert result.returncode == 0
    assert calls[0][:3] == ["docker", "run", "--name"]
    assert "--rm" not in calls[0]
    assert calls[1] == [
        "docker",
        "container",
        "inspect",
        "cad-bench-test",
    ]
    assert calls[2] == [
        "docker",
        "commit",
        "cad-bench-test",
        "cad-bench-after:cad-bench-test",
    ]
    assert calls[3] == [
        "docker",
        "save",
        "--output",
        str(temp_export_path),
        "cad-bench-after:cad-bench-test",
    ]
    assert calls[4] == ["docker", "image", "rm", "cad-bench-after:cad-bench-test"]
    assert calls[5] == ["docker", "rm", "-f", "cad-bench-test"]
    status = json.loads(
        bench._container_export_status_path(export_path).read_text(encoding="utf-8")
    )
    assert status["container_found_after_run"] is True
    assert status["saved_image"] is True
    assert status["exported"] is True
    assert status["removed_container"] is True
    inspect_step = next(
        step for step in status["steps"] if step["stage"] == "inspect_container"
    )
    assert inspect_step["stdout"] == "out"


def test_run_docker_command_exports_failed_run_when_container_exists(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    export_path = tmp_path / "container_after.tar"
    temp_export_path = export_path.with_name(export_path.name + ".tmp")

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(
                args=cmd, returncode=7, stdout="", stderr="agent failed"
            )
        if cmd[:2] == ["docker", "save"]:
            Path(cmd[3]).write_text("image", encoding="utf-8")
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="ok", stderr=""
        )

    monkeypatch.setattr(bench.subprocess, "run", fake_run)
    result = bench.run_docker_command(
        image="cad-build123d-bench:test",
        entrypoint="uv",
        mounts=[],
        env={},
        args=["run", "echo", "ok"],
        container_export_path=export_path,
        container_name="cad-bench-test",
    )

    assert result.returncode == 7
    assert calls[1] == [
        "docker",
        "container",
        "inspect",
        "cad-bench-test",
    ]
    assert calls[2][:2] == ["docker", "commit"]
    assert calls[3] == [
        "docker",
        "save",
        "--output",
        str(temp_export_path),
        "cad-bench-after:cad-bench-test",
    ]
    status = json.loads(
        bench._container_export_status_path(export_path).read_text(encoding="utf-8")
    )
    assert status["run"]["returncode"] == 7
    assert status["saved_image"] is True
    assert status["exported"] is True
    assert status["export_error"] is None


def test_run_docker_command_redacts_sensitive_envs_in_container_inspect_status(
    monkeypatch, tmp_path: Path
) -> None:
    export_path = tmp_path / "container_after.tar"

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["docker", "container", "inspect"]:
            stdout = json.dumps(
                [
                    {
                        "Config": {
                            "Env": [
                                "OPENAI_API_KEY=secret",
                                "HF_TOKEN=hf_secret",
                                "PATH=/usr/bin",
                            ]
                        }
                    }
                ]
            )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=stdout, stderr=""
            )
        if cmd[:2] == ["docker", "save"]:
            Path(cmd[3]).write_text("image", encoding="utf-8")
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="ok", stderr=""
        )

    monkeypatch.setattr(bench.subprocess, "run", fake_run)
    bench.run_docker_command(
        image="cad-build123d-bench:test",
        entrypoint="uv",
        mounts=[],
        env={},
        args=["run", "echo", "ok"],
        container_export_path=export_path,
        container_name="cad-bench-test",
    )

    status = json.loads(
        bench._container_export_status_path(export_path).read_text(encoding="utf-8")
    )
    inspect_step = next(
        step for step in status["steps"] if step["stage"] == "inspect_container"
    )
    assert "OPENAI_API_KEY=secret" not in inspect_step["stdout"]
    assert "HF_TOKEN=hf_secret" not in inspect_step["stdout"]
    assert "OPENAI_API_KEY=REDACTED" in inspect_step["stdout"]
    assert "HF_TOKEN=REDACTED" in inspect_step["stdout"]
    assert "PATH=/usr/bin" in inspect_step["stdout"]


def test_run_docker_command_retries_failed_image_save(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    export_path = tmp_path / "container_after.tar"
    temp_export_path = export_path.with_name(export_path.name + ".tmp")
    save_attempts = 0

    def fake_run(cmd, **kwargs):
        nonlocal save_attempts
        calls.append(cmd)
        if cmd[:2] == ["docker", "save"]:
            save_attempts += 1
            if save_attempts == 1:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=1, stdout="", stderr="transient save failure"
                )
            Path(cmd[3]).write_text("image", encoding="utf-8")
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="ok", stderr=""
        )

    monkeypatch.setattr(bench.subprocess, "run", fake_run)
    monkeypatch.setattr(bench.time, "sleep", lambda *_args, **_kwargs: None)
    result = bench.run_docker_command(
        image="cad-build123d-bench:test",
        entrypoint="uv",
        mounts=[],
        env={},
        args=["run", "echo", "ok"],
        container_export_path=export_path,
        container_name="cad-bench-test",
    )

    assert result.returncode == 0
    assert calls[3] == [
        "docker",
        "save",
        "--output",
        str(temp_export_path),
        "cad-bench-after:cad-bench-test",
    ]
    assert calls[4] == [
        "docker",
        "save",
        "--output",
        str(temp_export_path),
        "cad-bench-after:cad-bench-test",
    ]
    status = json.loads(
        bench._container_export_status_path(export_path).read_text(encoding="utf-8")
    )
    assert status["saved_image"] is True
    assert status["exported"] is True
    assert status["export_error"] is None
    assert [step["stage"] for step in status["steps"] if step["stage"].startswith("save_image_attempt_")] == [
        "save_image_attempt_1",
        "save_image_attempt_2",
    ]


def test_container_provenance_error_requires_uploaded_after_image() -> None:
    status = {
        "container_found_after_run": True,
        "committed_image": True,
        "saved_image": True,
        "removed_container": True,
        "exported": True,
        "export_error": None,
        "cleanup_error": None,
    }

    assert (
        bench._container_provenance_error(status, None)
        == "missing uploaded post-run container image"
    )


def test_container_provenance_error_surfaces_export_failure() -> None:
    status = {
        "container_found_after_run": False,
        "committed_image": False,
        "saved_image": False,
        "removed_container": True,
        "exported": False,
        "export_error": "docker commit failed",
        "cleanup_error": None,
    }

    assert (
        bench._container_provenance_error(status, "")
        == "post-run container export failed: docker commit failed"
    )


def test_summarize_container_export_status_omits_large_process_blobs() -> None:
    status = {
        "container_found_after_run": True,
        "committed_image": True,
        "saved_image": True,
        "removed_image": True,
        "removed_container": True,
        "exported": True,
        "export_size_bytes": 1234,
        "export_error": None,
        "cleanup_error": None,
        "run": {"returncode": 0, "stdout": "x" * 1000, "stderr": "y" * 1000},
        "steps": [
            {"stage": "inspect_container", "returncode": 0, "stdout": "blob"},
            {"stage": "commit_image", "returncode": 0, "stderr": "blob"},
        ],
    }

    summarized = bench._summarize_container_export_status(status)

    assert summarized == {
        "container_found_after_run": True,
        "committed_image": True,
        "saved_image": True,
        "removed_image": True,
        "removed_container": True,
        "exported": True,
        "export_size_bytes": 1234,
        "export_error": None,
        "cleanup_error": None,
        "run_returncode": 0,
        "steps": [
            {"stage": "inspect_container", "returncode": 0},
            {"stage": "commit_image", "returncode": 0},
        ],
    }


def test_default_container_image_requires_env(monkeypatch) -> None:
    def boom(name: str) -> str:
        raise RuntimeError(f"{name} is required in .env")

    monkeypatch.setattr(bench, "required_env_value", boom)
    with pytest.raises(RuntimeError, match="CAD_BENCH_AGENT_IMAGE"):
        bench.default_container_image()


def test_agent_step_codex_runs_in_container(monkeypatch, tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    submission_dir = tmp_path / "submission"
    auth_home = tmp_path / "auth" / ".codex"
    workdir.mkdir()
    submission_dir.mkdir()
    auth_home.mkdir(parents=True)
    captured: dict[str, object] = {}
    spec = load_builtin_harness("codex", "gpt-5.4", "web", "low")

    @contextmanager
    def fake_codex_runtime_home():
        yield auth_home

    monkeypatch.setattr(
        "harnesses.codex.utils.codex_runtime_home", fake_codex_runtime_home
    )

    def fake_run_docker_command(**kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout='{"type":"thread.started","thread_id":"thr_123"}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"<build123d_code>pass</build123d_code>"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":2}}\n',
            stderr="",
        )

    monkeypatch.setattr(bench, "run_docker_command", fake_run_docker_command)
    result = bench.run_harness(
        spec,
        system_prompt=None,
        prompt="task prompt",
        workdir=workdir,
        submission_dir=submission_dir,
        image="cad-build123d-bench:test",
    )

    assert result.error is None
    assert result.session_id == "thr_123"
    assert captured["image"] == "cad-build123d-bench:test"
    assert captured["entrypoint"] == "codex"
    mounts = captured["mounts"]
    assert bench.BindMount(workdir.resolve(), bench.CONTAINER_WORKDIR) in mounts
    assert (
        bench.BindMount(submission_dir.resolve(), bench.CONTAINER_SUBMISSION_DIR)
        in mounts
    )
    assert (
        bench.BindMount(
            (submission_dir / "final.step").resolve(), bench.CONTAINER_HOME_STEP_PATH
        )
        in mounts
    )
    assert (
        bench.BindMount(auth_home.resolve(), harness_utils.CONTAINER_CODEX_DIR)
        in mounts
    )
    assert captured["env"] == {
        "PYTHONPATH": harness_utils.CONTAINER_PROJECT_SITE_PACKAGES_DIR
    }
    args = captured["args"]
    assert args[:4] == [
        "--dangerously-bypass-approvals-and-sandbox",
        "--enable",
        "multi_agent",
        "--search",
    ]
    assert "exec" in args
    assert "-c" in args
    assert 'model_reasoning_effort="low"' in args
    assert bench.CONTAINER_WORKDIR in args
    assert "--workdir" in result.command
    assert bench.CONTAINER_WORKDIR in result.command


def test_agent_step_pi_offline_mounts_submission_and_docs(
    monkeypatch, tmp_path: Path
) -> None:
    workdir = tmp_path / "work"
    submission_dir = tmp_path / "submission"
    docs_dir = tmp_path / "docs"
    auth_home = tmp_path / "auth" / ".pi"
    site_packages_dir = tmp_path / "site-packages"
    workdir.mkdir()
    submission_dir.mkdir()
    docs_dir.mkdir()
    auth_home.mkdir(parents=True)
    (site_packages_dir / "build123d").mkdir(parents=True)
    (site_packages_dir / "build123d" / "__init__.py").write_text("", encoding="utf-8")
    captured: dict[str, object] = {}
    spec = load_builtin_harness("pi", "gpt-5.4", "offline", "high")

    @contextmanager
    def fake_pi_runtime_home(
        models, *, provider_name="codex-runtime"
    ):
        assert models[0]["id"] == "gpt-5.4"
        assert provider_name == "codex-runtime"
        yield auth_home

    monkeypatch.setattr(
        "harnesses.pi.utils.pi_runtime_home", fake_pi_runtime_home
    )
    monkeypatch.setattr(
        "harnesses.pi.utils.offline_agent_site_packages_dir",
        lambda: site_packages_dir.resolve(),
    )

    def fake_run_docker_command(**kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout='{"type":"session","id":"sess_123"}\n'
            '{"type":"turn_end","message":{"role":"assistant","content":[{"type":"text","text":"done"}],"usage":{"input_tokens":3,"output_tokens":4}}}\n',
            stderr="",
        )

    monkeypatch.setattr(bench, "run_docker_command", fake_run_docker_command)
    result = bench.run_harness(
        spec,
        system_prompt=None,
        prompt="task prompt",
        workdir=workdir,
        submission_dir=submission_dir,
        image="cad-build123d-bench:test",
        docs_dir=docs_dir,
    )

    assert result.error is None
    assert result.session_id == "sess_123"
    assert captured["entrypoint"] == "pi"
    mounts = captured["mounts"]
    assert bench.BindMount(workdir.resolve(), bench.CONTAINER_WORKDIR) in mounts
    assert (
        bench.BindMount(submission_dir.resolve(), bench.CONTAINER_SUBMISSION_DIR)
        in mounts
    )
    assert (
        bench.BindMount(
            (submission_dir / "final.step").resolve(), bench.CONTAINER_HOME_STEP_PATH
        )
        in mounts
    )
    assert (
        bench.BindMount(docs_dir.resolve(), bench.CONTAINER_DOCS_DIR, read_only=True)
        in mounts
    )
    assert (
        bench.BindMount(auth_home.resolve(), harness_utils.CONTAINER_PI_DIR) in mounts
    )
    assert (
        bench.BindMount(
            site_packages_dir.resolve(),
            harness_utils.CONTAINER_OFFLINE_SITE_PACKAGES_DIR,
            read_only=True,
        )
        in mounts
    )
    assert captured["env"] == {
        "PYTHONPATH": harness_utils.CONTAINER_OFFLINE_SITE_PACKAGES_DIR
    }
    args = captured["args"]
    assert args[:2] == ["--provider", "codex-runtime"]
    assert "--extension" not in args


def test_agent_step_pi_web_exposes_project_python_packages(
    monkeypatch, tmp_path: Path
) -> None:
    workdir = tmp_path / "work"
    submission_dir = tmp_path / "submission"
    auth_home = tmp_path / "auth" / ".pi"
    workdir.mkdir()
    submission_dir.mkdir()
    auth_home.mkdir(parents=True)
    captured: dict[str, object] = {}
    spec = load_builtin_harness("pi", "gpt-5.4", "web", "high")

    @contextmanager
    def fake_pi_runtime_home(models, *, provider_name="codex-runtime"):
        assert models[0]["id"] == "gpt-5.4"
        assert provider_name == "codex-runtime"
        yield auth_home

    monkeypatch.setattr("harnesses.pi.utils.pi_runtime_home", fake_pi_runtime_home)
    monkeypatch.setattr("harnesses.pi.utils.env_value", lambda name: "")

    def fake_run_docker_command(**kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout='{"type":"session","id":"sess_123"}\n'
            '{"type":"turn_end","message":{"role":"assistant","content":[{"type":"text","text":"done"}],"usage":{"input":3,"output":4}}}\n',
            stderr="",
        )

    monkeypatch.setattr(bench, "run_docker_command", fake_run_docker_command)
    result = bench.run_harness(
        spec,
        system_prompt=None,
        prompt="task prompt",
        workdir=workdir,
        submission_dir=submission_dir,
        image="cad-build123d-bench:test",
    )

    assert result.error is None
    assert captured["env"] == {
        "PYTHONPATH": harness_utils.CONTAINER_PROJECT_SITE_PACKAGES_DIR
    }
    args = captured["args"]
    assert "--extension" in args
    assert "--no-extensions" not in args
    assert "--thinking" in args
    assert "high" in args


def test_agent_step_pi_web_enables_pi_web_access_extension(
    monkeypatch, tmp_path: Path
) -> None:
    workdir = tmp_path / "work"
    submission_dir = tmp_path / "submission"
    auth_home = tmp_path / "auth" / ".pi"
    workdir.mkdir()
    submission_dir.mkdir()
    auth_home.mkdir(parents=True)
    captured: dict[str, object] = {}
    spec = load_builtin_harness("pi", "gpt-5.4", "web", "low")

    @contextmanager
    def fake_pi_runtime_home(models, *, provider_name="codex-runtime"):
        assert models[0]["id"] == "gpt-5.4"
        assert provider_name == "codex-runtime"
        yield auth_home

    monkeypatch.setattr("harnesses.pi.utils.pi_runtime_home", fake_pi_runtime_home)
    monkeypatch.setattr("harnesses.pi.utils.env_value", lambda name: "")

    def fake_run_docker_command(**kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout='{"type":"session","id":"sess_123"}\n'
            '{"type":"turn_end","message":{"role":"assistant","content":[{"type":"text","text":"done"}],"usage":{"input_tokens":3,"output_tokens":4}}}\n',
            stderr="",
        )

    monkeypatch.setattr(bench, "run_docker_command", fake_run_docker_command)
    result = bench.run_harness(
        spec,
        system_prompt=None,
        prompt="task prompt",
        workdir=workdir,
        submission_dir=submission_dir,
        image="cad-build123d-bench:test",
    )

    assert result.error is None
    assert captured["entrypoint"] == "pi"
    assert "EXA_API_KEY" not in captured["env"]
    args = captured["args"]
    assert "--no-extensions" not in args
    assert "--extension" in args
    extension_index = args.index("--extension")
    assert (
        args[extension_index + 1] == harness_utils.PI_WEB_EXTENSION_CONTAINER_PATH
    )


def test_agent_step_pi_aggregates_usage_across_turn_end_events(
    monkeypatch, tmp_path: Path
) -> None:
    workdir = tmp_path / "work"
    submission_dir = tmp_path / "submission"
    auth_home = tmp_path / "auth" / ".pi"
    workdir.mkdir()
    submission_dir.mkdir()
    auth_home.mkdir(parents=True)
    spec = load_builtin_harness("pi", "gpt-5.4", "offline", "low")

    @contextmanager
    def fake_pi_runtime_home(models, *, provider_name="codex-runtime"):
        assert models[0]["id"] == "gpt-5.4"
        assert provider_name == "codex-runtime"
        yield auth_home

    monkeypatch.setattr("harnesses.pi.utils.pi_runtime_home", fake_pi_runtime_home)
    monkeypatch.setattr(
        "harnesses.pi.utils.offline_agent_site_packages_dir",
        lambda: tmp_path.resolve(),
    )

    def fake_run_docker_command(**kwargs):
        del kwargs
        return subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout='{"type":"session","id":"sess_123"}\n'
            '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"first"}],"usage":{"input":100,"output":10,"cacheRead":0,"totalTokens":110}}}\n'
            '{"type":"turn_end","message":{"role":"assistant","content":[{"type":"text","text":"first"}],"usage":{"input":100,"output":10,"cacheRead":0,"totalTokens":110}}}\n'
            '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"second"}],"usage":{"input":20,"output":5,"cacheRead":80,"totalTokens":105}}}\n'
            '{"type":"turn_end","message":{"role":"assistant","content":[{"type":"text","text":"second"}],"usage":{"input":20,"output":5,"cacheRead":80,"totalTokens":105}}}\n',
            stderr="",
        )

    monkeypatch.setattr(bench, "run_docker_command", fake_run_docker_command)
    result = bench.run_harness(
        spec,
        system_prompt=None,
        prompt="task prompt",
        workdir=workdir,
        submission_dir=submission_dir,
        image="cad-build123d-bench:test",
    )

    assert result.error is None
    assert result.response == "second"
    assert result.usage == {
        "input": 120,
        "output": 15,
        "cacheRead": 80,
        "totalTokens": 215,
    }


def test_write_pi_runtime_home_sets_exa_provider_without_api_key(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "harnesses.pi.utils.codex_oauth_credentials",
        lambda: {"access": "token"},
    )

    target_dir = tmp_path / ".pi"
    pi_utils.write_pi_runtime_home(target_dir, models=[{"id": "gpt-5.4"}])

    assert (target_dir / "agent" / "models.json").exists()
    assert json.loads((target_dir / "web-search.json").read_text(encoding="utf-8")) == {
        "provider": "exa"
    }


def test_agent_step_codex_offline_mounts_python_packages(
    monkeypatch, tmp_path: Path
) -> None:
    workdir = tmp_path / "work"
    submission_dir = tmp_path / "submission"
    auth_home = tmp_path / "auth" / ".codex"
    site_packages_dir = tmp_path / "site-packages"
    workdir.mkdir()
    submission_dir.mkdir()
    auth_home.mkdir(parents=True)
    (site_packages_dir / "build123d").mkdir(parents=True)
    (site_packages_dir / "build123d" / "__init__.py").write_text("", encoding="utf-8")
    captured: dict[str, object] = {}
    spec = load_builtin_harness("codex", "gpt-5.4", "offline", "med")

    @contextmanager
    def fake_codex_runtime_home():
        yield auth_home

    monkeypatch.setattr(
        "harnesses.codex.utils.codex_runtime_home", fake_codex_runtime_home
    )
    monkeypatch.setattr(
        "harnesses.codex.utils.offline_agent_site_packages_dir",
        lambda: site_packages_dir.resolve(),
    )

    def fake_run_docker_command(**kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout='{"type":"thread.started","thread_id":"thr_456"}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"<build123d_code>pass</build123d_code>"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":2}}\n',
            stderr="",
        )

    monkeypatch.setattr(bench, "run_docker_command", fake_run_docker_command)
    result = bench.run_harness(
        spec,
        system_prompt=None,
        prompt="task prompt",
        workdir=workdir,
        submission_dir=submission_dir,
        image="cad-build123d-bench:test",
    )

    assert result.error is None
    mounts = captured["mounts"]
    assert (
        bench.BindMount(auth_home.resolve(), harness_utils.CONTAINER_CODEX_DIR)
        in mounts
    )
    assert (
        bench.BindMount(
            site_packages_dir.resolve(),
            harness_utils.CONTAINER_OFFLINE_SITE_PACKAGES_DIR,
            read_only=True,
        )
        in mounts
    )
    assert captured["env"] == {
        "PYTHONPATH": harness_utils.CONTAINER_OFFLINE_SITE_PACKAGES_DIR
    }


def test_agent_step_codex_web_exposes_project_python_packages(
    monkeypatch, tmp_path: Path
) -> None:
    workdir = tmp_path / "work"
    submission_dir = tmp_path / "submission"
    auth_home = tmp_path / "auth" / ".codex"
    workdir.mkdir()
    submission_dir.mkdir()
    auth_home.mkdir(parents=True)
    captured: dict[str, object] = {}
    spec = load_builtin_harness("codex", "gpt-5.4", "web", "med")

    @contextmanager
    def fake_codex_runtime_home():
        yield auth_home

    monkeypatch.setattr(
        "harnesses.codex.utils.codex_runtime_home", fake_codex_runtime_home
    )

    def fake_run_docker_command(**kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout='{"type":"thread.started","thread_id":"thr_456"}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":2}}\n',
            stderr="",
        )

    monkeypatch.setattr(bench, "run_docker_command", fake_run_docker_command)
    result = bench.run_harness(
        spec,
        system_prompt=None,
        prompt="task prompt",
        workdir=workdir,
        submission_dir=submission_dir,
        image="cad-build123d-bench:test",
    )

    assert result.error is None
    assert captured["env"] == {
        "PYTHONPATH": harness_utils.CONTAINER_PROJECT_SITE_PACKAGES_DIR
    }
    assert "--search" in captured["args"]


def test_sanitize_command_redacts_sensitive_env_values() -> None:
    command = [
        "docker",
        "run",
        "-e",
        "OPENAI_API_KEY=secret",
        "--env",
        "HF_TOKEN=hf_secret",
        "-e",
        "PATH=/usr/bin",
    ]

    assert harness_utils.sanitize_command_for_logging(command) == [
        "docker",
        "run",
        "-e",
        "OPENAI_API_KEY=REDACTED",
        "--env",
        "HF_TOKEN=REDACTED",
        "-e",
        "PATH=/usr/bin",
    ]


def test_sanitize_env_var_spec_redacts_only_sensitive_values() -> None:
    assert harness_utils.sanitize_env_var_spec("OPENAI_API_KEY=secret") == (
        "OPENAI_API_KEY=REDACTED"
    )
    assert harness_utils.sanitize_env_var_spec("HF_TOKEN=hf_secret") == (
        "HF_TOKEN=REDACTED"
    )
    assert harness_utils.sanitize_env_var_spec("PATH=/usr/bin") == "PATH=/usr/bin"


def test_openai_harness_logs_status_history_without_exposing_api_key(
    monkeypatch, tmp_path: Path
) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    spec = load_builtin_harness("openai", "gpt-5.4", "offline", "high")
    monkeypatch.setattr(
        "harnesses.openai.utils.required_env_value",
        lambda name: "secret" if name == "OPENAI_API_KEY" else "",
    )

    def fake_run_docker_command(**kwargs):
        (workdir / ".openai_response_status.jsonl").write_text(
            '{"status":"queued"}\n{"status":"completed"}\n',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout=json.dumps(
                {
                    "response": "<build123d_code>part = Box(1, 1, 1)</build123d_code>",
                    "response_id": "resp_123",
                    "usage": {
                        "input_tokens": 10,
                        "cached_input_tokens": 0,
                        "output_tokens": 5,
                    },
                    "raw_response": {"status": "completed"},
                    "status_history_path": ".openai_response_status.jsonl",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(bench, "run_docker_command", fake_run_docker_command)
    result = bench.run_harness(
        spec,
        system_prompt=harness_utils.XML_CODE_SYSTEM_PROMPT,
        prompt="task prompt",
        workdir=workdir,
        submission_dir=None,
        image="cad-build123d-bench:test",
    )

    assert result.error is None
    assert result.command.count("OPENAI_API_KEY=REDACTED") == 1
    assert "OPENAI_API_KEY=secret" not in result.command
    assert result.artifact_contents["openai_response_status.jsonl"].count("status") == 2


def test_one_shot_openai_runs_in_container(monkeypatch, tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setattr(
        "harnesses.openai.utils.required_env_value",
        lambda name: "secret" if name == "OPENAI_API_KEY" else "",
    )
    captured: dict[str, object] = {}
    spec = load_builtin_harness("openai", "gpt-5.4", "web", "med")

    def fake_run_docker_command(**kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout='{"response":"<build123d_code>pass</build123d_code>","response_id":"resp_123","usage":{"input_tokens":5,"output_tokens":7,"total_tokens":12},"raw_response":{"id":"resp_123","output":[]}}',
            stderr="",
        )

    monkeypatch.setattr(bench, "run_docker_command", fake_run_docker_command)
    result = bench.run_harness(
        spec,
        system_prompt="system prompt",
        prompt="task prompt",
        workdir=workdir,
        submission_dir=None,
        image="cad-build123d-bench:test",
    )

    assert result.error is None
    assert result.session_id == "resp_123"
    assert captured["entrypoint"] == "uv"
    assert captured["workdir"] == bench.CONTAINER_APP_DIR
    assert captured["env"]["OPENAI_API_KEY"] == "secret"
    assert not (workdir / ".openai_request.json").exists()


def test_one_shot_openai_non_completed_status_is_error(
    monkeypatch, tmp_path: Path
) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setattr(
        "harnesses.openai.utils.required_env_value",
        lambda name: "secret" if name == "OPENAI_API_KEY" else "",
    )
    spec = load_builtin_harness("openai", "gpt-5.4", "web", "med")

    def fake_run_docker_command(**kwargs):
        del kwargs
        return subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout='{"response":"","response_id":"resp_123","usage":{"input_tokens":5,"output_tokens":7,"total_tokens":12},"raw_response":{"id":"resp_123","status":"failed","output":[]}}',
            stderr="",
        )

    monkeypatch.setattr(bench, "run_docker_command", fake_run_docker_command)
    result = bench.run_harness(
        spec,
        system_prompt="system prompt",
        prompt="task prompt",
        workdir=workdir,
        submission_dir=None,
        image="cad-build123d-bench:test",
    )

    assert result.error == "openai response ended with status=failed"


def test_one_shot_api_model_runs_in_container(monkeypatch, tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setattr(
        "harnesses.api_models.utils.env_value",
        lambda name, default="": {
            "DEEPSEEK_API_KEY": "secret",
            "DEEPSEEK_BASE_URL": "https://example.test/v1",
        }.get(name, default),
    )
    captured: dict[str, object] = {}
    spec = load_builtin_harness("deepseek", "deepseek-v4-pro", "offline", "none")

    def fake_run_docker_command(**kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout=json.dumps(
                {
                    "response": "<build123d_code>pass</build123d_code>",
                    "response_id": "chatcmpl_123",
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 7,
                        "total_tokens": 12,
                    },
                    "raw_response": {"id": "chatcmpl_123"},
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(bench, "run_docker_command", fake_run_docker_command)
    result = bench.run_harness(
        spec,
        system_prompt="system prompt",
        prompt="task prompt",
        workdir=workdir,
        submission_dir=None,
        image="cad-build123d-bench:test",
    )

    assert result.error is None
    assert result.session_id == "chatcmpl_123"
    assert captured["entrypoint"] == "uv"
    assert captured["workdir"] == bench.CONTAINER_APP_DIR
    assert captured["env"]["CAD_BENCH_API_KEY"] == "secret"
    assert result.command.count("CAD_BENCH_API_KEY=REDACTED") == 1
    assert not (workdir / ".deepseek_request.json").exists()


def test_api_model_key_can_come_from_process_environment(
    monkeypatch, tmp_path: Path
) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "process_secret")
    monkeypatch.setattr("harnesses.api_models.utils.env_value", lambda name, default="": default)
    captured: dict[str, object] = {}
    spec = load_builtin_harness("deepseek", "deepseek-v4-flash", "offline", "none")

    def fake_run_docker_command(**kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout=json.dumps(
                {
                    "response": "<build123d_code>pass</build123d_code>",
                    "response_id": "chatcmpl_123",
                    "usage": {"prompt_tokens": 5, "completion_tokens": 7},
                    "raw_response": {"id": "chatcmpl_123"},
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(bench, "run_docker_command", fake_run_docker_command)
    result = bench.run_harness(
        spec,
        system_prompt="system prompt",
        prompt="task prompt",
        workdir=workdir,
        submission_dir=None,
        image="cad-build123d-bench:test",
    )

    assert result.error is None
    assert captured["env"]["CAD_BENCH_API_KEY"] == "process_secret"


def test_openai_codex_api_model_uses_codex_oauth(
    monkeypatch, tmp_path: Path
) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setattr(
        "harnesses.api_models.utils._codex_oauth_credentials",
        lambda: {"access": "codex_access", "accountId": "account_123"},
    )
    captured: dict[str, object] = {}
    spec = load_builtin_harness("openai_codex", "gpt-5.5", "offline", "high")

    def fake_run_docker_command(**kwargs):
        captured.update(kwargs)
        config_arg_index = kwargs["args"].index("--config") + 1
        config_path = Path(
            str(kwargs["args"][config_arg_index]).replace("/workspace/", "")
        )
        captured["request_config"] = json.loads((workdir / config_path).read_text())
        return subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout=json.dumps(
                {
                    "response": "<build123d_code>pass</build123d_code>",
                    "response_id": "resp_123",
                    "usage": {"input_tokens": 5, "output_tokens": 7},
                    "raw_response": {"id": "resp_123"},
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(bench, "run_docker_command", fake_run_docker_command)
    result = bench.run_harness(
        spec,
        system_prompt="system prompt",
        prompt="task prompt",
        workdir=workdir,
        submission_dir=None,
        image="cad-build123d-bench:test",
    )

    assert result.error is None
    assert captured["env"]["CAD_BENCH_API_KEY"] == "codex_access"
    assert result.command.count("CAD_BENCH_API_KEY=REDACTED") == 1
    assert captured["request_config"]["reasoning_effort"] == "high"
    assert not (workdir / ".openai_codex_request.json").exists()


def test_openai_codex_api_model_falls_back_to_local_codex_auth(
    monkeypatch,
) -> None:
    from harnesses.api_models import utils as api_utils

    def stale_env_auth():
        raise RuntimeError("stale env auth")

    monkeypatch.setattr("harnesses.codex.utils.codex_oauth_credentials", stale_env_auth)
    monkeypatch.setattr(
        "harnesses.codex.utils.local_codex_oauth_credentials",
        lambda: {"access": "local_access", "accountId": "account_123"},
    )

    assert api_utils._codex_oauth_credentials()["access"] == "local_access"


def test_one_shot_code_export_runs_in_networkless_container(
    monkeypatch, tmp_path: Path
) -> None:
    submission_dir = tmp_path / "submission"
    submission_dir.mkdir()
    step_path = submission_dir / "final.step"
    captured: dict[str, object] = {}

    def fake_run_docker_command(**kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            args=["docker"], returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(bench, "run_docker_command", fake_run_docker_command)
    export_result = bench._export_submission_step_result(
        "cube_20mm_z_minus",
        "part = Box(20, 20, 20)",
        step_path,
        image="cad-build123d-bench:test",
    )

    assert export_result["error"] == ""
    assert export_result["process"]["returncode"] == 0
    assert captured["image"] == "cad-build123d-bench:test"
    assert captured["entrypoint"] == "uv"
    assert captured["workdir"] == bench.CONTAINER_APP_DIR
    assert captured["network"] == "none"
    assert (
        bench.BindMount(submission_dir.resolve(), bench.CONTAINER_SUBMISSION_DIR)
        in captured["mounts"]
    )
    assert not (submission_dir / ".export_payload.json").exists()
