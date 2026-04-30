from pathlib import Path

from build123d import export_step
import pytest

from bench import runner as bench
from conftest import builtin_harness, load_builtin_harness
from harnesses import utils as harness_utils
from tasks import utils as task_utils

M3_TASK_ID = "m3x6_socket_head_zminus"
M3_SPEC = bench.TASK_DEFS[M3_TASK_ID]


def test_compose_instruction_switches_web_mode() -> None:
    prompt = "Create one cube."
    web_text = bench.compose_step_submission_prompt(
        task_prompt=prompt,
        access_mode="web",
    )
    docs_text = bench.compose_step_submission_prompt(
        task_prompt=prompt,
        access_mode="offline",
    )
    assert "Internet access is available" in web_text
    assert "Internet access is not available" in docs_text
    assert "`python` is installed" in docs_text
    assert "`build123d` installed and importable" in docs_text
    assert bench.CONTAINER_DOCS_DIR in docs_text
    assert "/workspace/final.step" in web_text
    assert "Build123D or any other solution" in web_text


def test_transient_agent_error_detection_uses_provider_error_messages() -> None:
    spec = harness_utils.harness(
        "pi/gpt-5.4-web-high",
        provider="pi",
        strategy="agent_step",
        model="gpt-5.4",
    )
    result = harness_utils.AgentRunResult(
        response="",
        error="pi did not emit usage data",
        stdout="",
        stderr="",
        returncode=0,
        command=["pi"],
        session_id="sess_1",
        usage={},
        parsed_events=[
            {
                "type": "turn_end",
                "errorMessage": 'Codex error: {"type":"error","error":{"type":"server_error"}}',
            }
        ],
    )

    assert bench._agent_error_messages(result.parsed_events) == [
        'Codex error: {"type":"error","error":{"type":"server_error"}}'
    ]
    assert bench._is_transient_agent_error(spec, result) is True


def test_transient_agent_error_detection_ignores_non_provider_failures() -> None:
    spec = harness_utils.harness(
        "pi/gpt-5.4-web-high",
        provider="pi",
        strategy="agent_step",
        model="gpt-5.4",
    )
    result = harness_utils.AgentRunResult(
        response="",
        error="pi did not emit usage data",
        stdout="",
        stderr="",
        returncode=0,
        command=["pi"],
        session_id="sess_1",
        usage={},
        parsed_events=[{"type": "turn_end", "errorMessage": "missing_submission"}],
    )

    assert bench._is_transient_agent_error(spec, result) is False


def test_generate_docs_bundle_top_only(tmp_path: Path) -> None:
    out = bench.generate_build123d_docs_bundle(
        tmp_path / "docs", include_submodules=False
    )
    assert (out / "index.md").exists()
    assert (out / "single_turn.md").exists()
    start_here = out / "modules" / "00_start_here.md"
    assert start_here.exists()
    assert "Recommended path" in start_here.read_text(encoding="utf-8")
    assert list((out / "modules").glob("*.md"))


def test_score_step_submission_reference(tmp_path: Path) -> None:
    task_id = "box_30x20x10_z_plus"
    spec = bench.TASK_DEFS[task_id]
    ns: dict[str, object] = {}
    exec(spec.reference_solution_code, {}, ns)
    step_path = tmp_path / "box.step"
    export_step(ns["part"], step_path)
    metrics = bench.score_step_submission(task_id, step_path)
    assert metrics["build_success"] == 1.0
    assert metrics["reward"] > 0.95


def test_score_from_step_records_bad_step_as_failed_submission(tmp_path: Path) -> None:
    step_path = tmp_path / "bad.step"
    step_path.write_text("not a step file\n", encoding="utf-8")

    metrics = bench.score_from_step("cube_20mm_z_minus", step_path)

    assert metrics["submission_exists"] == 1.0
    assert metrics["build_success"] == 0.0
    assert metrics["reward"] == 0.0
    assert metrics["overall_score"] == 0.0
    assert metrics["task_score"] == 0.0
    assert metrics["score_error"] == "Cannot tessellate an empty shape"


def test_score_step_submission_records_mesh_failures_in_metrics(
    monkeypatch, tmp_path: Path
) -> None:
    task_id = "cube_20mm_z_minus"
    spec = bench.TASK_DEFS[task_id]
    ns: dict[str, object] = {}
    exec(spec.reference_solution_code, {}, ns)
    step_path = tmp_path / "cube.step"
    export_step(ns["part"], step_path)

    def boom(part, tolerance: float = 0.03):  # type: ignore[no-untyped-def]
        del part, tolerance
        raise ValueError("Tessellation produced no mesh")

    monkeypatch.setattr(task_utils, "mesh_from_part", boom)

    metrics = bench.score_step_submission(task_id, step_path)

    assert metrics["build_success"] == 0.0
    assert metrics["reward"] == 0.0
    assert metrics["overall_score"] == 0.0
    assert metrics["task_score"] == 0.0
    assert metrics["failure_stage"] == "mesh_error"
    assert metrics["error_type"] == "ValueError"
    assert metrics["error_message"] == "Tessellation produced no mesh"


def test_score_from_step_propagates_non_submission_runtime_errors(monkeypatch) -> None:
    def boom(task_id: str, step_path: Path) -> dict[str, object]:
        del task_id, step_path
        raise RuntimeError("blender is missing")

    monkeypatch.setattr(bench, "score_step_submission", boom)
    with pytest.raises(RuntimeError, match="blender is missing"):
        bench.score_from_step("cube_20mm_z_minus", Path("/tmp/final.step"))


def test_score_step_submission_m3_reference(tmp_path: Path) -> None:
    step_path = tmp_path / "m3.step"
    part = task_utils.execute_candidate_code(M3_SPEC.reference_solution_code)
    export_step(part, step_path)
    metrics = bench.score_step_submission(M3_TASK_ID, step_path)
    assert metrics["build_success"] == 1.0
    assert metrics["reward"] > 0.75


def test_harness_spec_catalog_and_prompt_shapes(tmp_path: Path) -> None:
    one_shot = load_builtin_harness("openai", "gpt-5.4", "web", "low")
    system_prompt, prompt = bench.build_harness_prompt(one_shot, "Create one box.")
    assert system_prompt == harness_utils.XML_CODE_SYSTEM_PROMPT
    assert "Write Build123D code to create the model." in prompt
    assert "<build123d_code>" in prompt

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "index.md").write_text("Index\n", encoding="utf-8")
    (docs_dir / "single_turn.md").write_text("Builder docs bundle\n", encoding="utf-8")
    offline = load_builtin_harness("openai", "gpt-5.4", "offline", "med")
    original = harness_utils.ensure_build123d_docs_bundle
    harness_utils.ensure_build123d_docs_bundle = lambda cache_dir=str(docs_dir): (
        docs_dir
    )
    try:
        system_prompt, prompt = bench.build_harness_prompt(offline, "Create one box.")
    finally:
        harness_utils.ensure_build123d_docs_bundle = original
    assert system_prompt == harness_utils.XML_CODE_SYSTEM_PROMPT
    assert "<build123d_docs>" in prompt
    assert "Builder docs bundle" in prompt
    assert "Create one box." in prompt

    web_ci = load_builtin_harness("openai", "gpt-5.4", "web_ci", "xhigh")
    system_prompt, prompt = bench.build_harness_prompt(web_ci, "Create one box.")
    assert system_prompt == harness_utils.XML_CODE_SYSTEM_PROMPT
    assert "built-in code interpreter" in prompt

    gemma_nodocs = load_builtin_harness(
        "gemini", "gemma-3-27b-it", "offline_nodocs", "none"
    )
    system_prompt, prompt = bench.build_harness_prompt(gemma_nodocs, "Create one box.")
    assert system_prompt is None
    assert "<build123d_docs>" not in prompt
    assert "Builder docs bundle" not in prompt
    assert "Do not use triple backticks." in prompt
    assert "Create one box." in prompt


def test_task_selection_includes_m3_like_any_other_task() -> None:
    tasks = bench.select_task_specs(
        task_ids=["cube_20mm_z_minus", "m3x6_socket_head_zminus"]
    )
    assert [task.task_id for task in tasks] == [
        "cube_20mm_z_minus",
        "m3x6_socket_head_zminus",
    ]


def test_harness_paths_are_derived_not_checked_in() -> None:
    refs = [
        builtin_harness("pi", "gpt-5.4", "web", "low"),
        builtin_harness("pi", "gpt-5.4", "offline", "xhigh"),
        builtin_harness("codex", "gpt-5.4", "web", "low"),
        builtin_harness("codex", "gpt-5.3-codex-spark", "offline", "xhigh"),
        builtin_harness("openai", "gpt-5.4", "web", "low"),
        builtin_harness("openai", "gpt-5.4-mini", "offline", "xhigh"),
        builtin_harness("openai", "gpt-5.4-nano", "web", "med"),
        builtin_harness("openai", "gpt-5.4", "web_ci", "xhigh"),
    ]
    for ref in refs:
        spec = bench.load_harness_spec(ref)
        assert spec.file_path.endswith("/harnesses.py")
        assert spec.symbol_name


def test_invalid_submission_scores_zero_reward() -> None:
    metrics = bench.score_code_submission("cube_20mm_z_minus", "")
    assert metrics["build_success"] == 0.0
    assert metrics["reward"] == 0.0


def test_candidate_code_cannot_import_benchmark_internals() -> None:
    exploit = (
        "u = __import__('tasks.utils')\n"
        "part = u._reference_part_for_task('cube_20mm_z_minus')\n"
    )
    metrics = bench.score_code_submission(
        "cube_20mm_z_minus", exploit, llm_generated=False
    )

    assert metrics["build_success"] == 0.0
    assert metrics["reward"] == 0.0
    assert metrics["failure_stage"] == "execution_error"
    assert metrics["error_type"] == "ImportError"
    assert "tasks.utils" in metrics["error_message"]


def test_llm_generated_code_watchdog_interrupts_infinite_loop() -> None:
    with pytest.raises(TimeoutError, match="LLM-generated code exceeded"):
        task_utils.execute_llm_generated_code(
            "while True:\n    pass\n",
            timeout_seconds=0.05,
        )


def test_score_code_submission_propagates_scoring_failures(monkeypatch) -> None:
    def boom(task_id: str, code: str):
        raise RuntimeError("tessellation failed")

    monkeypatch.setattr(bench, "_evaluate_code_for_task_cached", boom)
    with pytest.raises(RuntimeError, match="tessellation failed"):
        bench.score_code_submission("cube_20mm_z_minus", "part = None")
