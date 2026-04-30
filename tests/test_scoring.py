import textwrap

from bench import runner as root
from harnesses import utils as harness_utils
from tasks import utils as task_utils
from tasks.specs import task_meta, task_dir

TASK_ID = "m3x6_socket_head_zminus"
TASK = root.TASK_DEFS[TASK_ID]
REFERENCE_STEP_FIXTURE = task_dir(TASK_ID) / str(task_meta(TASK_ID)["reference_step_fixture"])


def test_reference_solution_scores_one() -> None:
    metrics = root._evaluate_for_task_cached(TASK_ID, TASK.reference_solution_code)
    assert metrics["build_success"] == 1.0
    assert metrics["reward"] > 0.99


def test_score_ladder_covers_tiers() -> None:
    invalid_metrics = root._evaluate_for_task_cached(
        TASK_ID, "part = this_is_not_valid_python("
    )
    assert invalid_metrics["build_success"] == 0.0
    assert invalid_metrics["reward"] == 0.0
    assert invalid_metrics["failure_stage"] == "execution_error"
    assert invalid_metrics["error_type"] == "SyntaxError"

    tier_cases = {
        "tier_025": (
            textwrap.dedent(
                """
                from build123d import *
                part = Cylinder(1.5, 6.0, align=(Align.CENTER, Align.CENTER, Align.MAX))
                """
            ).strip(),
            None,
        ),
        "tier_050": (
            textwrap.dedent(
                """
                from build123d import *
                head = Cylinder(2.75, 3.0, align=(Align.CENTER, Align.CENTER, Align.MAX))
                shank = Cylinder(1.5, 6.0, align=(Align.CENTER, Align.CENTER, Align.MAX)).located(Location((0, 0, -3)))
                with BuildPart() as socket_tool:
                    with BuildSketch(Plane.XY):
                        RegularPolygon(radius=2.5 / (3 ** 0.5), side_count=6)
                    extrude(amount=-1.5)
                part = head + shank - socket_tool.part
                """
            ).strip(),
            None,
        ),
        "tier_075": (
            textwrap.dedent(
                """
                from build123d import *
                MAJOR_D = 3.0
                PITCH = 0.5
                head = Cylinder(5.5 / 2, 3.0, align=(Align.CENTER, Align.CENTER, Align.MAX))
                shank = Cylinder(MAJOR_D / 2, 6.0, align=(Align.CENTER, Align.CENTER, Align.MAX)).located(Location((0, 0, -3)))
                with BuildLine() as bl:
                    helix = Helix(
                        pitch=PITCH,
                        height=5.6,
                        radius=MAJOR_D / 2 - 0.01,
                        center=(0, 0, -3 - 6 + 0.2),
                        direction=(0, 0, 1),
                        lefthand=False,
                    )
                edge = helix.edges()[0]
                with BuildSketch(edge.location_at(0.0)) as thread_profile:
                    Circle(0.12)
                thread = sweep(sections=thread_profile.sketch.face(), path=edge, is_frenet=True)
                part = head + shank + thread
                """
            ).strip(),
            None,
        ),
        "tier_100": (
            TASK.reference_solution_code,
            0.99,
        ),
    }

    rewards = {
        name: root._evaluate_for_task_cached(TASK_ID, code)["reward"]
        for name, (code, _) in tier_cases.items()
    }
    assert rewards["tier_025"] < rewards["tier_050"] < rewards["tier_075"] < rewards["tier_100"]
    assert rewards["tier_025"] > 0.25
    assert rewards["tier_100"] > 0.99


def test_mcmaster_step_scores_one(tmp_path) -> None:
    step_path = tmp_path / "91290A111_mcmaster.step"
    step_path.write_bytes(REFERENCE_STEP_FIXTURE.read_bytes())

    code = textwrap.dedent(
        f"""
        from build123d import *
        part = import_step({step_path.as_posix()!r})
        part = part.located(Location((0, 0, -part.bounding_box().max.Z)))
        """
    ).strip()
    metrics = root._evaluate_for_task_cached(TASK_ID, code)
    assert metrics["build_success"] == 1.0
    assert metrics["reward"] > 0.99


def test_parser_requires_build123d_code_tag() -> None:
    parser = harness_utils.CADCodeParser()
    completion = [{"role": "assistant", "content": "<code>part = Box(1,1,1)</code>"}]
    extracted = parser.parse_answer(completion)
    assert extracted is None


def test_invalid_code_raises() -> None:
    metrics = root._evaluate_for_task_cached(
        TASK_ID, "part = this_is_not_valid_python("
    )
    assert metrics["build_success"] == 0.0
    assert metrics["reward"] == 0.0
    assert metrics["failure_stage"] == "execution_error"
    assert metrics["error_type"] == "SyntaxError"


def test_score_code_submission_accepts_single_unambiguous_build123d_variable() -> None:
    metrics = root.score_code_submission(
        "cube_20mm_z_minus",
        textwrap.dedent(
            """
            from build123d import *
            cube = Box(20, 20, 20, align=(Align.CENTER, Align.CENTER, Align.MAX))
            """
        ).strip(),
    )

    assert metrics["build_success"] == 1.0
    assert metrics["reward"] == 1.0


def test_score_code_submission_accepts_single_unambiguous_buildpart_variable() -> None:
    metrics = root.score_code_submission(
        "cube_20mm_z_minus",
        textwrap.dedent(
            """
            from build123d import *
            with BuildPart() as cube:
                Box(20, 20, 20, align=(Align.CENTER, Align.CENTER, Align.MAX))
            """
        ).strip(),
    )

    assert metrics["build_success"] == 1.0
    assert metrics["reward"] == 1.0


def test_execute_candidate_code_rejects_ambiguous_build123d_variables() -> None:
    code = textwrap.dedent(
        """
        from build123d import *
        a = Box(20, 20, 20)
        b = Box(10, 10, 10)
        """
    ).strip()

    try:
        task_utils.execute_candidate_code(code)
    except ValueError as exc:
        assert "single unambiguous Build123D object variable" in str(exc)
    else:
        raise AssertionError("ambiguous Build123D variables should be rejected")


def test_load_benchmark_contract() -> None:
    benchmark = root.load_benchmark(task_ids=[TASK_ID])
    assert benchmark is not None
    assert benchmark.examples
    row = benchmark.examples[0]
    assert "reference_solution_code" not in row["answer"]
    assert row["answer"]["task_id"] == TASK_ID
    assert row["info"]["difficulty"] == "hard"
    assert row["info"]["mode"] == "single_turn"


def test_pose_errors_reduce_m3_reward() -> None:
    shifted = TASK.reference_solution_code + "\npart = part.located(Location((0, 0, 10.0)))\n"
    metrics = root._evaluate_for_task_cached(TASK_ID, shifted)
    assert metrics["reward"] < 0.7
    assert metrics["overall_score"] < 0.7
