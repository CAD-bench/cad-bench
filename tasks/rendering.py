from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from tasks.specs import TaskModuleSpec
from tasks.utils import (
    _blender_pythonpath_args,
    _slug,
    _task_asset_path,
    combine_components,
    execute_candidate_code,
    export_mesh_stl,
    gear_layer_metrics,
    gearbox_phase_trials,
    gearbox_sim_transfer_score,
    mesh_from_part,
    select_compound_gearbox_components,
    select_gearbox_components,
    select_right_angle_gearbox_solids,
)


def _blender_bin() -> str:
    blender = shutil.which("blender")
    if blender is None:
        raise RuntimeError(
            "Blender is required to render reference videos but was not found on PATH"
        )
    return blender


def render_pair_gearbox_reference_video(
    task: TaskModuleSpec, out_dir: Path
) -> tuple[Path, Path]:
    part = execute_candidate_code(task.reference_solution_code)
    selection = select_gearbox_components(part, task.expected)
    if (
        selection.failure_reason
        or selection.input_mesh is None
        or selection.output_mesh is None
    ):
        raise RuntimeError(
            f"{task.task_id}: could not select gearbox components: {selection.failure_reason or 'unknown'}"
        )

    slug = _slug(task.task_id)
    script_path = _task_asset_path(task.task_id, "blender_sim.py")
    video_path = out_dir / f"cad_bench__{slug}.mp4"
    metrics_path = out_dir / f"cad_bench__{slug}.sim.json"
    max_faces = int(max(256, task.expected["max_sim_faces"]))
    z_mid = 0.5 * (float(task.expected["z_min_mm"]) + float(task.expected["z_max_mm"]))
    output_metrics = gear_layer_metrics(
        selection.output_mesh,
        (float(task.expected["axle_b_x_mm"]), float(task.expected["axle_b_y_mm"])),
        z_mid,
        task.expected,
    )
    phase_trials = gearbox_phase_trials(float(output_metrics["tooth_count"]))

    with tempfile.TemporaryDirectory(prefix=f"{slug}_") as td:
        tmp = Path(td)
        gear_a_stl = tmp / "gear_a.stl"
        gear_b_stl = tmp / "gear_b.stl"
        export_mesh_stl(selection.input_mesh, gear_a_stl, max_faces=max_faces)
        export_mesh_stl(selection.output_mesh, gear_b_stl, max_faces=max_faces)

        best_phase = 0.0
        best_score = -1.0
        for idx, phase_b_deg in enumerate(phase_trials):
            probe_json = tmp / f"probe_{idx}.json"
            probe_cmd = [
                _blender_bin(),
                "--background",
                *_blender_pythonpath_args(),
                "--python",
                script_path.as_posix(),
                "--",
                "--gear-a-stl",
                gear_a_stl.as_posix(),
                "--gear-b-stl",
                gear_b_stl.as_posix(),
                "--out-json",
                probe_json.as_posix(),
                "--input-rpm",
                str(float(task.expected["input_rpm"])),
                "--target-output-rpm",
                str(float(task.expected["target_output_rpm"])),
                "--axle-a-x-mm",
                str(float(task.expected["axle_a_x_mm"])),
                "--axle-a-y-mm",
                str(float(task.expected["axle_a_y_mm"])),
                "--axle-b-x-mm",
                str(float(task.expected["axle_b_x_mm"])),
                "--axle-b-y-mm",
                str(float(task.expected["axle_b_y_mm"])),
                "--axle-radius-mm",
                str(float(task.expected["axle_radius_mm"])),
                "--axle-flat-x-from-center-mm",
                str(float(task.expected["axle_flat_x_from_center_mm"])),
                "--axle-z-min-mm",
                str(float(task.expected["axle_min_z_mm"])),
                "--axle-z-max-mm",
                str(float(task.expected["axle_max_z_mm"])),
                "--max-sim-faces",
                str(max_faces),
                "--drive-rpm-scale",
                str(float(task.expected["drive_rpm_scale"])),
                "--sim-fps",
                str(int(float(task.expected["sim_fps"]))),
                "--sim-seconds",
                str(float(task.expected["sim_seconds"])),
                "--rb-substeps",
                str(int(float(task.expected["rb_substeps"]))),
                "--rb-iterations",
                str(int(float(task.expected["rb_iterations"]))),
                "--voxel-remesh-mm",
                str(float(task.expected["voxel_remesh_mm"])),
                "--phase-a-deg",
                "0.0",
                "--phase-b-deg",
                f"{float(phase_b_deg):.6f}",
            ]
            subprocess.run(
                probe_cmd,
                capture_output=True,
                text=True,
                check=True,
            )
            score = float(
                gearbox_sim_transfer_score(
                    json.loads(probe_json.read_text(encoding="utf-8"))
                )
            )
            if score > best_score:
                best_phase = float(phase_b_deg)
                best_score = score

        render_cmd = [
            _blender_bin(),
            "--background",
            *_blender_pythonpath_args(),
            "--python",
            script_path.as_posix(),
            "--",
            "--gear-a-stl",
            gear_a_stl.as_posix(),
            "--gear-b-stl",
            gear_b_stl.as_posix(),
            "--out-json",
            metrics_path.as_posix(),
            "--render-mp4",
            video_path.as_posix(),
            "--input-rpm",
            str(float(task.expected["input_rpm"])),
            "--target-output-rpm",
            str(float(task.expected["target_output_rpm"])),
            "--axle-a-x-mm",
            str(float(task.expected["axle_a_x_mm"])),
            "--axle-a-y-mm",
            str(float(task.expected["axle_a_y_mm"])),
            "--axle-b-x-mm",
            str(float(task.expected["axle_b_x_mm"])),
            "--axle-b-y-mm",
            str(float(task.expected["axle_b_y_mm"])),
            "--axle-radius-mm",
            str(float(task.expected["axle_radius_mm"])),
            "--axle-flat-x-from-center-mm",
            str(float(task.expected["axle_flat_x_from_center_mm"])),
            "--axle-z-min-mm",
            str(float(task.expected["axle_min_z_mm"])),
            "--axle-z-max-mm",
            str(float(task.expected["axle_max_z_mm"])),
            "--max-sim-faces",
            str(max_faces),
            "--drive-rpm-scale",
            str(float(task.expected["drive_rpm_scale"])),
            "--sim-fps",
            str(int(float(task.expected["sim_fps"]))),
            "--sim-seconds",
            str(float(task.expected["sim_seconds"])),
            "--rb-substeps",
            str(int(float(task.expected["rb_substeps"]))),
            "--rb-iterations",
            str(int(float(task.expected["rb_iterations"]))),
            "--voxel-remesh-mm",
            str(float(task.expected["voxel_remesh_mm"])),
            "--phase-a-deg",
            "0.0",
            "--phase-b-deg",
            f"{best_phase:.6f}",
        ]
        subprocess.run(
            render_cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    return video_path, metrics_path


def render_compound_reference_video(
    task: TaskModuleSpec, out_dir: Path
) -> tuple[Path, Path]:
    part = execute_candidate_code(task.reference_solution_code)
    selection = select_compound_gearbox_components(part, task.expected)
    if (
        selection.failure_reason
        or selection.input_mesh is None
        or selection.middle_mesh is None
        or selection.output_mesh is None
    ):
        raise RuntimeError(
            f"{task.task_id}: could not select compound gearbox components: {selection.failure_reason or 'unknown'}"
        )

    slug = _slug(task.task_id)
    script_path = _task_asset_path(task.task_id, "blender_sim.py")
    video_path = out_dir / f"cad_bench__{slug}.mp4"
    metrics_path = out_dir / f"cad_bench__{slug}.sim.json"
    max_faces = int(max(256, task.expected["max_sim_faces"]))

    with tempfile.TemporaryDirectory(prefix=f"{slug}_") as td:
        tmp = Path(td)
        gear_a_stl = tmp / "gear_a.stl"
        gear_b_stl = tmp / "gear_b.stl"
        gear_c_stl = tmp / "gear_c.stl"
        export_mesh_stl(selection.input_mesh, gear_a_stl, max_faces=max_faces)
        export_mesh_stl(selection.middle_mesh, gear_b_stl, max_faces=max_faces)
        export_mesh_stl(selection.output_mesh, gear_c_stl, max_faces=max_faces)
        cmd = [
            _blender_bin(),
            "--background",
            *_blender_pythonpath_args(),
            "--python",
            script_path.as_posix(),
            "--",
            "--gear-a-stl",
            gear_a_stl.as_posix(),
            "--gear-b-stl",
            gear_b_stl.as_posix(),
            "--gear-c-stl",
            gear_c_stl.as_posix(),
            "--out-json",
            metrics_path.as_posix(),
            "--render-mp4",
            video_path.as_posix(),
            "--input-rpm",
            str(float(task.expected["input_rpm"])),
            "--target-output-rpm",
            str(float(task.expected["target_output_rpm"])),
            "--axle-a-x-mm",
            str(float(task.expected["axle_a_x_mm"])),
            "--axle-a-y-mm",
            str(float(task.expected["axle_a_y_mm"])),
            "--axle-b-x-mm",
            str(float(task.expected["axle_b_x_mm"])),
            "--axle-b-y-mm",
            str(float(task.expected["axle_b_y_mm"])),
            "--axle-c-x-mm",
            str(float(task.expected["axle_c_x_mm"])),
            "--axle-c-y-mm",
            str(float(task.expected["axle_c_y_mm"])),
            "--axle-radius-mm",
            str(float(task.expected["axle_radius_mm"])),
            "--axle-flat-x-from-center-mm",
            str(float(task.expected["axle_flat_x_from_center_mm"])),
            "--axle-z-min-mm",
            str(float(task.expected["axle_min_z_mm"])),
            "--axle-z-max-mm",
            str(float(task.expected["axle_max_z_mm"])),
            "--max-sim-faces",
            str(max_faces),
            "--drive-rpm-scale",
            str(float(task.expected["drive_rpm_scale"])),
            "--drive-mode",
            str(task.expected["drive_mode"]),
            "--sim-fps",
            str(int(float(task.expected["sim_fps"]))),
            "--sim-seconds",
            str(float(task.expected["sim_seconds"])),
            "--rb-substeps",
            str(int(float(task.expected["rb_substeps"]))),
            "--rb-iterations",
            str(int(float(task.expected["rb_iterations"]))),
            "--mesh-collision-margin",
            str(float(task.expected["mesh_collision_margin"])),
            "--voxel-remesh-mm",
            str(float(task.expected["voxel_remesh_mm"])),
            "--render-frame-step",
            "5",
            "--phase-a-deg",
            "0.0",
            "--phase-b-deg",
            "0.0",
            "--phase-c-deg",
            "0.0",
        ]
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    return video_path, metrics_path


def render_right_angle_reference_video(
    task: TaskModuleSpec, out_dir: Path
) -> tuple[Path, Path]:
    part = execute_candidate_code(task.reference_solution_code)
    selection = select_right_angle_gearbox_solids(part, task.expected)
    if selection.failure_reason:
        raise RuntimeError(
            f"{task.task_id}: could not classify right-angle gearbox solids: {selection.failure_reason}"
        )

    compound_mesh = combine_components(
        [mesh_from_part(solid) for solid in selection.compound_solids]
    )
    if compound_mesh is None:
        raise RuntimeError(f"{task.task_id}: missing compound mesh")

    slug = _slug(task.task_id)
    script_path = _task_asset_path(task.task_id, "blender_sim.py")
    video_path = out_dir / f"cad_bench__{slug}.mp4"
    metrics_path = out_dir / f"cad_bench__{slug}.sim.json"
    max_faces = int(max(256, task.expected["max_sim_faces"]))

    with tempfile.TemporaryDirectory(prefix=f"{slug}_") as td:
        tmp = Path(td)
        gear_a_stl = tmp / "gear_a.stl"
        gear_b_stl = tmp / "gear_b.stl"
        gear_c_stl = tmp / "gear_c.stl"
        export_mesh_stl(
            mesh_from_part(selection.input_solids[0]), gear_a_stl, max_faces=max_faces
        )
        export_mesh_stl(compound_mesh, gear_b_stl, max_faces=max_faces)
        export_mesh_stl(
            mesh_from_part(selection.output_solids[0]), gear_c_stl, max_faces=max_faces
        )
        cmd = [
            _blender_bin(),
            "--background",
            *_blender_pythonpath_args(),
            "--python",
            script_path.as_posix(),
            "--",
            "--gear-a-stl",
            gear_a_stl.as_posix(),
            "--gear-b-stl",
            gear_b_stl.as_posix(),
            "--gear-c-stl",
            gear_c_stl.as_posix(),
            "--out-json",
            metrics_path.as_posix(),
            "--render-mp4",
            video_path.as_posix(),
            "--input-rpm",
            str(float(task.expected["input_rpm"])),
            "--target-output-rpm",
            str(float(task.expected["target_output_rpm"])),
            "--input-axle-x-mm",
            str(float(task.expected["input_axle_x_mm"])),
            "--input-axle-y-mm",
            str(float(task.expected["input_axle_y_mm"])),
            "--input-axle-z-min-mm",
            str(float(task.expected["input_axle_z_min_mm"])),
            "--input-axle-z-max-mm",
            str(float(task.expected["input_axle_z_max_mm"])),
            "--compound-axle-x-min-mm",
            str(float(task.expected["compound_axle_x_min_mm"])),
            "--compound-axle-x-max-mm",
            str(float(task.expected["compound_axle_x_max_mm"])),
            "--compound-axle-y-mm",
            str(float(task.expected["compound_axle_y_mm"])),
            "--compound-axle-z-mm",
            str(float(task.expected["compound_axle_z_mm"])),
            "--output-axle-x-mm",
            str(float(task.expected["output_axle_x_mm"])),
            "--output-axle-y-min-mm",
            str(float(task.expected["output_axle_y_min_mm"])),
            "--output-axle-y-max-mm",
            str(float(task.expected["output_axle_y_max_mm"])),
            "--output-axle-z-mm",
            str(float(task.expected["output_axle_z_mm"])),
            "--max-sim-faces",
            str(max_faces),
            "--drive-rpm-scale",
            str(float(task.expected["drive_rpm_scale"])),
            "--drive-mode",
            str(task.expected["drive_mode"]),
            "--motor-max-impulse",
            str(float(task.expected["motor_max_impulse"])),
            "--gear-b-mass-kg",
            str(float(task.expected["gear_b_mass_kg"])),
            "--gear-c-mass-kg",
            str(float(task.expected["gear_c_mass_kg"])),
            "--rb-linear-damping",
            str(float(task.expected["rb_linear_damping"])),
            "--rb-angular-damping",
            str(float(task.expected["rb_angular_damping"])),
            "--sim-fps",
            str(int(float(task.expected["sim_fps"]))),
            "--sim-seconds",
            str(float(task.expected["sim_seconds"])),
            "--rb-substeps",
            str(int(float(task.expected["rb_substeps"]))),
            "--rb-iterations",
            str(int(float(task.expected["rb_iterations"]))),
            "--mesh-collision-margin",
            str(float(task.expected["mesh_collision_margin"])),
            "--voxel-remesh-mm",
            str(float(task.expected["voxel_remesh_mm"])),
            "--shaft-radius-mm",
            str(float(task.expected["axle_radius_mm"])),
            "--render-frame-step",
            "4",
            "--phase-a-deg",
            "0.0",
            "--phase-b-deg",
            "0.0",
            "--phase-c-deg",
            "0.0",
        ]
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    return video_path, metrics_path
