import pytest

from bench import runner as bench


def test_load_environment_has_tasks() -> None:
    benchmark = bench.load_benchmark()
    assert benchmark is not None
    assert benchmark.examples
    ids = [row["answer"]["task_id"] for row in benchmark.examples]
    assert "cube_20mm_z_minus" in ids
    assert "box_30x20x10_z_plus" in ids
    assert "aluminum_extrusion_20x20x60_5_bore" in ids
    assert "spur_gear_m1_z16_t6_bore5" in ids
    assert "l_bracket_40x40x6_2_hole" in ids
    assert "flange_40_24_h20_bolt4" in ids
    assert "ring_30_20_h10_z_plus" in ids
    assert "plate_80x40x8_slot_4_hole" in ids
    assert "hex_nut_af17_h8_bore10" in ids
    assert "ring_50_30_h12_6_bolt4_pcd40" in ids
    assert "step_block_60_40_30_4_hole" in ids
    assert "plate_100x60x12_slot_8_hole" in ids
    assert "m3x6_socket_head_zminus" in ids
    assert "gearbox_functional_collision_trim" in ids
    assert "custom_threaded_pair_nonstandard" in ids
    assert "compound_gearbox_functional" in ids
    assert "compound_right_angle_gearbox_reverse" in ids
    assert "reference_solution_code" not in benchmark.examples[0]["answer"]


def test_difficulty_metadata_present() -> None:
    benchmark = bench.load_benchmark()
    by_id = {row["answer"]["task_id"]: row for row in benchmark.examples}
    assert by_id["box_30x20x10_z_plus"]["info"]["difficulty"] == "easy"
    assert by_id["cube_20mm_z_minus"]["info"]["difficulty"] == "easy"
    assert by_id["l_bracket_40x40x6_2_hole"]["info"]["difficulty"] == "medium"
    assert by_id["flange_40_24_h20_bolt4"]["info"]["difficulty"] == "hard"
    assert by_id["ring_30_20_h10_z_plus"]["info"]["difficulty"] == "easy"
    assert by_id["plate_80x40x8_slot_4_hole"]["info"]["difficulty"] == "medium"
    assert by_id["hex_nut_af17_h8_bore10"]["info"]["difficulty"] == "medium"
    assert by_id["ring_50_30_h12_6_bolt4_pcd40"]["info"]["difficulty"] == "hard"
    assert by_id["step_block_60_40_30_4_hole"]["info"]["difficulty"] == "hard"
    assert by_id["plate_100x60x12_slot_8_hole"]["info"]["difficulty"] == "hard"
    assert by_id["m3x6_socket_head_zminus"]["info"]["difficulty"] == "hard"
    assert by_id["gearbox_functional_collision_trim"]["info"]["difficulty"] == "insane"
    assert by_id["custom_threaded_pair_nonstandard"]["info"]["difficulty"] == "insane"
    assert by_id["compound_gearbox_functional"]["info"]["difficulty"] == "insane"
    assert by_id["compound_right_angle_gearbox_reverse"]["info"]["difficulty"] == "insane"
    assert {row["info"]["difficulty"] for row in benchmark.examples} <= {"easy", "medium", "hard", "insane"}


def test_task_order_progresses_by_difficulty() -> None:
    benchmark = bench.load_benchmark()
    ids = [row["answer"]["task_id"] for row in benchmark.examples]
    assert ids == [
        "cube_20mm_z_minus",
        "box_30x20x10_z_plus",
        "ring_30_20_h10_z_plus",
        "l_bracket_40x40x6_2_hole",
        "aluminum_extrusion_20x20x60_5_bore",
        "plate_80x40x8_slot_4_hole",
        "hex_nut_af17_h8_bore10",
        "flange_40_24_h20_bolt4",
        "ring_50_30_h12_6_bolt4_pcd40",
        "step_block_60_40_30_4_hole",
        "plate_100x60x12_slot_8_hole",
        "spur_gear_m1_z16_t6_bore5",
        "m3x6_socket_head_zminus",
        "gearbox_functional_collision_trim",
        "custom_threaded_pair_nonstandard",
        "compound_gearbox_functional",
        "compound_right_angle_gearbox_reverse",
    ]


def test_default_benchmark_prompt() -> None:
    benchmark = bench.load_benchmark()
    question = benchmark.examples[0]["question"]
    assert "Write Build123D code to create the model." in question
    assert "<build123d_code>" in question
    assert benchmark.examples[0]["info"]["mode"] == "single_turn"


def test_reference_solutions_score_high() -> None:
    for task_id, spec in bench.TASK_DEFS.items():
        metrics = bench._evaluate_for_task_cached(task_id, spec.reference_solution_code)
        assert metrics["build_success"] == 1.0
        assert metrics["reward"] > 0.95


def test_bad_scores_lower() -> None:
    bad_code = "from build123d import *\npart = Cylinder(5, 10, align=(Align.CENTER, Align.CENTER, Align.MAX))"
    for task_id in bench.TASK_DEFS:
        metrics = bench._evaluate_for_task_cached(task_id, bad_code)
        ref = bench._evaluate_for_task_cached(task_id, bench.TASK_DEFS[task_id].reference_solution_code)
        assert metrics["build_success"] == 1.0
        assert metrics["reward"] < max(0.85, ref["reward"] - 0.15)


def test_pose_errors_reduce_easy_reward() -> None:
    shifted_cube = (
        "from build123d import *\n"
        "part = Box(20.0, 20.0, 20.0, align=(Align.CENTER, Align.CENTER, Align.MAX)).located(Location((50.0, 50.0, 0.0)))"
    )
    wrong_side_cube = (
        "from build123d import *\n"
        "part = Box(20.0, 20.0, 20.0, align=(Align.CENTER, Align.CENTER, Align.MIN))"
    )
    shifted_box = (
        "from build123d import *\n"
        "part = Box(30.0, 20.0, 10.0, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((50.0, 50.0, 0.0)))"
    )

    shifted_cube_metrics = bench._evaluate_for_task_cached("cube_20mm_z_minus", shifted_cube)
    wrong_side_metrics = bench._evaluate_for_task_cached("cube_20mm_z_minus", wrong_side_cube)
    shifted_box_metrics = bench._evaluate_for_task_cached("box_30x20x10_z_plus", shifted_box)

    assert shifted_cube_metrics["reward"] < 0.30
    assert wrong_side_metrics["reward"] < 0.55
    assert shifted_box_metrics["reward"] < 0.32
    assert shifted_cube_metrics["overall_score"] < 0.20


def test_reference_geometry_gate_penalizes_simple_hole_omissions() -> None:
    bad_flange = (
        "from build123d import *\n"
        "base = Cylinder(20.0, 8.0, align=(Align.CENTER, Align.CENTER, Align.MIN))\n"
        "boss = Cylinder(12.0, 12.0, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((0, 0, 8.0)))\n"
        "part = base + boss\n"
        "part = part - Cylinder(6.0, 20.0, align=(Align.CENTER, Align.CENTER, Align.MIN))\n"
    )
    metrics = bench._evaluate_for_task_cached("flange_40_24_h20_bolt4", bad_flange)

    assert metrics["build_success"] == 1.0
    assert metrics["reference_geometry_gate"] < 0.65
    assert metrics["overall_score"] < 0.40


def test_geometric_reference_solutions_keep_high_overall_scores() -> None:
    geometric_task_ids = [
        "cube_20mm_z_minus",
        "box_30x20x10_z_plus",
        "ring_30_20_h10_z_plus",
        "l_bracket_40x40x6_2_hole",
        "aluminum_extrusion_20x20x60_5_bore",
        "plate_80x40x8_slot_4_hole",
        "hex_nut_af17_h8_bore10",
        "flange_40_24_h20_bolt4",
        "ring_50_30_h12_6_bolt4_pcd40",
        "step_block_60_40_30_4_hole",
        "plate_100x60x12_slot_8_hole",
        "spur_gear_m1_z16_t6_bore5",
    ]
    for task_id in geometric_task_ids:
        metrics = bench._evaluate_for_task_cached(task_id, bench.TASK_DEFS[task_id].reference_solution_code)
        assert metrics["build_success"] == 1.0
        assert metrics["overall_score"] >= 0.95
        assert metrics["pose_score"] >= 0.99


@pytest.mark.parametrize(
    ("task_id", "candidate_code", "max_overall_score"),
    [
        (
            "ring_30_20_h10_z_plus",
            (
                "from build123d import *\n"
                "part = Cylinder(15.0, 10.0, align=(Align.CENTER, Align.CENTER, Align.MIN))\n"
            ),
            0.50,
        ),
        (
            "aluminum_extrusion_20x20x60_5_bore",
            (
                "from build123d import *\n"
                "part = Box(20.0, 20.0, 60.0, align=(Align.CENTER, Align.CENTER, Align.MIN))"
                " - Cylinder(2.5, 60.0, align=(Align.CENTER, Align.CENTER, Align.MIN))\n"
            ),
            0.55,
        ),
        (
            "flange_40_24_h20_bolt4",
            (
                "from build123d import *\n"
                "base = Cylinder(20.0, 8.0, align=(Align.CENTER, Align.CENTER, Align.MIN))\n"
                "boss = Cylinder(12.0, 12.0, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((0, 0, 8.0)))\n"
                "part = base + boss\n"
                "part = part - Cylinder(6.0, 20.0, align=(Align.CENTER, Align.CENTER, Align.MIN))\n"
            ),
            0.55,
        ),
        (
            "step_block_60_40_30_4_hole",
            (
                "from build123d import *\n"
                "base = Box(60.0, 40.0, 10.0, align=(Align.CENTER, Align.CENTER, Align.MIN))\n"
                "mid = Box(40.0, 30.0, 10.0, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((0.0, 0.0, 10.0)))\n"
                "top = Box(20.0, 20.0, 10.0, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((0.0, 0.0, 20.0)))\n"
                "part = base + mid + top\n"
            ),
            0.55,
        ),
        (
            "plate_100x60x12_slot_8_hole",
            (
                "from build123d import *\n"
                "part = Box(100.0, 60.0, 12.0, align=(Align.CENTER, Align.CENTER, Align.MIN))\n"
                "part = part - Box(50.0, 12.0, 12.2, align=(Align.CENTER, Align.CENTER, Align.MIN))\n"
            ),
            0.60,
        ),
    ],
)
def test_obvious_feature_omissions_do_not_score_like_near_passes(task_id: str, candidate_code: str, max_overall_score: float) -> None:
    metrics = bench._evaluate_for_task_cached(task_id, candidate_code)
    ref_metrics = bench._evaluate_for_task_cached(task_id, bench.TASK_DEFS[task_id].reference_solution_code)
    assert metrics["build_success"] == 1.0
    assert metrics["overall_score"] <= max_overall_score
    assert ref_metrics["overall_score"] - metrics["overall_score"] >= 0.25

def test_gear_shallow_notch_profile_scores_lower() -> None:
    malformed_gear = """
from build123d import *

core = Cylinder(9.0, 6.0, align=(Align.CENTER, Align.CENTER, Align.MAX))

with BuildPart() as notch_cuts:
    with PolarLocations(8.6, 16):
        Cylinder(0.4, 6.2, align=(Align.CENTER, Align.CENTER, Align.MAX))

part = core - notch_cuts.part
part = part - Cylinder(2.5, 6.2, align=(Align.CENTER, Align.CENTER, Align.MAX))
""".strip()
    metrics = bench._evaluate_for_task_cached("spur_gear_m1_z16_t6_bore5", malformed_gear)
    assert metrics["build_success"] == 1.0
    assert metrics["reward"] < 0.75


def test_gearbox_reference_and_bad_candidate() -> None:
    task_id = "gearbox_functional_collision_trim"
    ref_metrics = bench._evaluate_for_task_cached(task_id, bench.TASK_DEFS[task_id].reference_solution_code)
    assert ref_metrics["build_success"] == 1.0
    assert ref_metrics["task_score"] > 0.50

    colliding_candidate = """
from build123d import *

g1 = Cylinder(13.333, 10.0, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((0.0, 0.0, 0.0)))
g2 = Cylinder(26.667, 10.0, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((40.0, 0.0, 0.0)))
g2 = g2 - Cylinder(2.4, 10.2, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((40.0, 0.0, 0.0)))
part = g1 + g2
""".strip()
    metrics = bench._evaluate_for_task_cached(task_id, colliding_candidate)
    assert metrics["build_success"] == 1.0
    assert metrics["task_score"] < 0.2
    assert metrics["reward"] < ref_metrics["reward"] - 0.2


def test_gearbox_rigid_bridge_fails_functional_transfer() -> None:
    task_id = "gearbox_functional_collision_trim"
    rigid_bridge = """
from build123d import *

part = Box(46.0, 8.0, 10.0, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((20.0, 0.0, 0.0)))
part = part - Cylinder(2.4, 10.2, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((0.0, 0.0, 0.0)))
part = part - Cylinder(2.4, 10.2, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((40.0, 0.0, 0.0)))
""".strip()
    metrics = bench._evaluate_for_task_cached(task_id, rigid_bridge)
    assert metrics["build_success"] == 1.0
    assert metrics["task_score"] < 0.3
    assert metrics["failure_reason"]


def test_gearbox_shifted_reference_does_not_get_recentered_credit() -> None:
    task_id = "gearbox_functional_collision_trim"
    shifted = (
        bench.TASK_DEFS[task_id].reference_solution_code
        + "\npart = part.located(Location((6.0, 0.0, 0.0)))\n"
    )
    metrics = bench._evaluate_for_task_cached(task_id, shifted)
    assert metrics["build_success"] == 1.0
    assert metrics["task_score"] < 0.2
    assert metrics["failure_reason"]


def test_custom_thread_reference_and_plain_pair() -> None:
    task_id = "custom_threaded_pair_nonstandard"
    ref_metrics = bench._evaluate_for_task_cached(task_id, bench.TASK_DEFS[task_id].reference_solution_code)
    assert ref_metrics["build_success"] == 1.0
    assert ref_metrics["task_score"] > 0.65

    plain_pair = """
from build123d import *

bolt = Cylinder(3.65, 14.2, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((-14.0, 0.0, 0.0)))
bolt = bolt + Cylinder(5.75, 3.6, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((-14.0, 0.0, 14.2)))
nut = Cylinder(6.5, 8.0, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((14.0, 0.0, 0.0)))
nut = nut - Cylinder(3.18, 8.2, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((14.0, 0.0, 0.0)))
part = Compound([bolt, nut])
""".strip()
    metrics = bench._evaluate_for_task_cached(task_id, plain_pair)
    assert metrics["build_success"] == 1.0
    assert metrics["task_score"] < ref_metrics["task_score"] - 0.20


def test_compound_gearbox_reference_and_bad_candidate() -> None:
    task_id = "compound_gearbox_functional"
    ref_metrics = bench._evaluate_for_task_cached(task_id, bench.TASK_DEFS[task_id].reference_solution_code)
    assert ref_metrics["build_success"] == 1.0
    assert ref_metrics["task_score"] > 0.45

    rigid_bridge = """
from build123d import *
part = Box(60.0, 8.0, 10.0, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((27.0, 0.0, 0.0)))
part = part - Cylinder(2.4, 10.2, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((0.0, 0.0, 0.0)))
part = part - Cylinder(2.4, 10.2, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((28.0, 0.0, 0.0)))
part = part - Cylinder(2.4, 10.2, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((54.0, 0.0, 0.0)))
""".strip()
    metrics = bench._evaluate_for_task_cached(task_id, rigid_bridge)
    assert metrics["build_success"] == 1.0
    assert metrics["task_score"] < ref_metrics["task_score"] - 0.20


def test_right_angle_gearbox_reference_and_bad_candidate() -> None:
    task_id = "compound_right_angle_gearbox_reverse"
    ref_metrics = bench._evaluate_for_task_cached(task_id, bench.TASK_DEFS[task_id].reference_solution_code)
    assert ref_metrics["build_success"] == 1.0
    assert ref_metrics["task_score"] > 0.45
    assert ref_metrics["reward"] == pytest.approx(0.05 + 0.95 * ref_metrics["task_score"])

    bad_candidate = """
from build123d import *
g1 = Cylinder(9.0, 6.0, align=(Align.CENTER, Align.CENTER, Align.CENTER)).located(Location((14.0, 0.0, 18.0)))
g2 = Cylinder(12.0, 6.0, align=(Align.CENTER, Align.CENTER, Align.CENTER)).located(Location((28.0, 0.0, 18.0)))
part = Compound([g1, g2])
""".strip()
    metrics = bench._evaluate_for_task_cached(task_id, bad_candidate)
    assert metrics["build_success"] == 1.0
    assert metrics["task_score"] < ref_metrics["task_score"] - 0.20

    intersecting_candidate = bench.TASK_DEFS[task_id].reference_solution_code.replace("CLEAR = 0.0", "CLEAR = -0.7", 1)
    metrics = bench._evaluate_for_task_cached(task_id, intersecting_candidate)
    assert metrics["build_success"] == 1.0
    assert metrics["failure_reason"] == "intersecting_bodies"
    assert metrics["task_score"] == 0.0
