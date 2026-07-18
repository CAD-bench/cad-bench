from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import math
import sys
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector

sys.path.append(str(Path(__file__).resolve().parents[1]))
from blender_common import (
    add_axle as _add_axle,
    add_hinge as _add_hinge,
    add_motor as _add_motor,
    add_rotation_pointer as _add_rotation_pointer,
    axis_basis as _axis_basis,
    fit_rpm as _fit_rpm,
    import_stl as _import_stl,
    instant_rpms as _instant_rpms,
    look_at as _look_at,
    maybe_decimate_mesh as _maybe_decimate_mesh,
    maybe_voxel_remesh as _maybe_voxel_remesh,
    parse_blender_args as _parse_blender_args,
    phase_rotation as _phase_rotation,
    render_multicam_mp4 as _render_multicam_mp4,
    score_near as _score_near,
    set_mesh_origin as _set_mesh_origin,
    setup_active_rigidbody as _setup_active_rigidbody,
    setup_passive_mesh_rigidbody as _setup_passive_mesh_rigidbody,
    window_rpms as _window_rpms,
)

def _parse_args() -> argparse.Namespace:
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = argv[1:]
    parser = argparse.ArgumentParser(description="Headless Blender rigid-body simulation for a compound right-angle gearbox")
    parser.add_argument("--gear-a-stl", required=True)
    parser.add_argument("--gear-b-stl", required=True)
    parser.add_argument("--gear-c-stl", required=True)
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--render-mp4", default="")
    parser.add_argument("--input-rpm", type=float, required=True)
    parser.add_argument("--target-output-rpm", type=float, required=True)
    parser.add_argument("--input-axle-x-mm", type=float, required=True)
    parser.add_argument("--input-axle-y-mm", type=float, required=True)
    parser.add_argument("--input-axle-z-min-mm", type=float, required=True)
    parser.add_argument("--input-axle-z-max-mm", type=float, required=True)
    parser.add_argument("--compound-axle-x-min-mm", type=float, required=True)
    parser.add_argument("--compound-axle-x-max-mm", type=float, required=True)
    parser.add_argument("--compound-axle-y-mm", type=float, required=True)
    parser.add_argument("--compound-axle-z-mm", type=float, required=True)
    parser.add_argument("--output-axle-x-mm", type=float, required=True)
    parser.add_argument("--output-axle-y-min-mm", type=float, required=True)
    parser.add_argument("--output-axle-y-max-mm", type=float, required=True)
    parser.add_argument("--output-axle-z-mm", type=float, required=True)
    parser.add_argument("--shaft-radius-mm", type=float, required=True)
    parser.add_argument("--max-sim-faces", type=int, default=3600)
    parser.add_argument("--voxel-remesh-mm", type=float, default=0.0)
    parser.add_argument("--phase-a-deg", type=float, default=0.0)
    parser.add_argument("--phase-b-deg", type=float, default=0.0)
    parser.add_argument("--phase-c-deg", type=float, default=0.0)
    parser.add_argument("--gear-b-mass-kg", type=float, default=0.12)
    parser.add_argument("--gear-c-mass-kg", type=float, default=0.03)
    parser.add_argument("--rb-friction", type=float, default=0.75)
    parser.add_argument("--rb-linear-damping", type=float, default=0.08)
    parser.add_argument("--rb-angular-damping", type=float, default=0.05)
    parser.add_argument("--sim-fps", type=int, default=60)
    parser.add_argument("--sim-seconds", type=float, default=2.4)
    parser.add_argument("--rb-substeps", type=int, default=8)
    parser.add_argument("--rb-iterations", type=int, default=96)
    parser.add_argument("--drive-rpm-scale", type=float, default=0.20)
    parser.add_argument("--drive-mode", choices=["keyframe", "motor"], default="keyframe")
    parser.add_argument("--motor-max-impulse", type=float, default=0.18)
    parser.add_argument("--mesh-collision-margin", type=float, default=0.00035)
    parser.add_argument("--render-slowdown", type=float, default=2.5)
    return _parse_blender_args(parser)



def _project_radius(vertices_world: np.ndarray, center_m: tuple[float, float, float], axis: str) -> float:
    center = np.asarray(center_m, dtype=float)
    rel = vertices_world - center
    if axis == "x":
        radial = np.linalg.norm(rel[:, 1:3], axis=1)
    elif axis == "y":
        radial = np.linalg.norm(rel[:, [0, 2]], axis=1)
    elif axis == "z":
        radial = np.linalg.norm(rel[:, :2], axis=1)
    else:
        raise ValueError(f"unsupported axis: {axis}")
    return float(np.quantile(radial, 0.995))



def _pointer_angle(pointer_obj: bpy.types.Object, parent_obj: bpy.types.Object, axis: str) -> float:
    e1, e2, _ = _axis_basis(axis)
    delta = pointer_obj.matrix_world.translation - parent_obj.matrix_world.translation
    return math.atan2(float(delta.dot(e2)), float(delta.dot(e1)))



def main() -> None:
    args = _parse_args()
    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out: dict[str, float | str] = {
        "mid_rpm": 0.0,
        "output_rpm": 0.0,
        "direction_score": 0.0,
        "speed_score": 0.0,
        "ratio_score": 0.0,
        "engaged": 0.0,
        "stop_fraction": 1.0,
        "sim_error": "",
    }
    with nullcontext():
        bpy.ops.wm.read_factory_settings(use_empty=True)
        scene = bpy.context.scene
        scene.unit_settings.system = "METRIC"
        scene.unit_settings.scale_length = 1.0
        scene.gravity = (0.0, 0.0, 0.0)
        scene.frame_start = 1
        scene.render.fps = int(max(24, args.sim_fps))
        scene.frame_end = int(max(12, args.sim_fps * args.sim_seconds))

        bpy.ops.rigidbody.world_add()
        scene.rigidbody_world.time_scale = 1.0
        scene.rigidbody_world.substeps_per_frame = max(2, int(args.rb_substeps))
        scene.rigidbody_world.solver_iterations = max(10, int(args.rb_iterations))
        cache = scene.rigidbody_world.point_cache
        cache.frame_start = int(scene.frame_start)
        cache.frame_end = int(scene.frame_end)

        scale = 0.01
        input_center = (
            float(args.input_axle_x_mm),
            float(args.input_axle_y_mm),
            0.5 * (float(args.input_axle_z_min_mm) + float(args.input_axle_z_max_mm)),
        )
        compound_center = (
            0.5 * (float(args.compound_axle_x_min_mm) + float(args.compound_axle_x_max_mm)),
            float(args.compound_axle_y_mm),
            float(args.compound_axle_z_mm),
        )
        output_center = (
            float(args.output_axle_x_mm),
            0.5 * (float(args.output_axle_y_min_mm) + float(args.output_axle_y_max_mm)),
            float(args.output_axle_z_mm),
        )

        axle_a = _add_axle(
            "InputAxle",
            "z",
            input_center,
            float(args.input_axle_z_max_mm) - float(args.input_axle_z_min_mm),
            float(args.shaft_radius_mm),
            scale,
        )
        axle_b = _add_axle(
            "CompoundAxle",
            "x",
            compound_center,
            float(args.compound_axle_x_max_mm) - float(args.compound_axle_x_min_mm),
            float(args.shaft_radius_mm),
            scale,
        )
        axle_c = _add_axle(
            "OutputAxle",
            "y",
            output_center,
            float(args.output_axle_y_max_mm) - float(args.output_axle_y_min_mm),
            float(args.shaft_radius_mm),
            scale,
        )

        gear_a = _import_stl(Path(args.gear_a_stl), "GearA", scale)
        gear_b = _import_stl(Path(args.gear_b_stl), "GearB", scale)
        gear_c = _import_stl(Path(args.gear_c_stl), "GearC", scale)
        for obj in (gear_a, gear_b, gear_c):
            _maybe_voxel_remesh(obj, float(args.voxel_remesh_mm), scale)
            _maybe_decimate_mesh(obj, int(args.max_sim_faces))

        _set_mesh_origin(gear_a, tuple(float(v) * scale for v in input_center))
        _set_mesh_origin(gear_b, tuple(float(v) * scale for v in compound_center))
        _set_mesh_origin(gear_c, tuple(float(v) * scale for v in output_center))
        gear_a.rotation_euler = _phase_rotation("z", float(args.phase_a_deg))
        gear_b.rotation_euler = _phase_rotation("x", float(args.phase_b_deg))
        gear_c.rotation_euler = _phase_rotation("y", float(args.phase_c_deg))
        bpy.context.view_layer.update()

        va = np.asarray([(gear_a.matrix_world @ v.co)[:] for v in gear_a.data.vertices], dtype=float)
        vb = np.asarray([(gear_b.matrix_world @ v.co)[:] for v in gear_b.data.vertices], dtype=float)
        vc = np.asarray([(gear_c.matrix_world @ v.co)[:] for v in gear_c.data.vertices], dtype=float)
        tip_a = _project_radius(va, tuple(float(v) * scale for v in input_center), "z")
        tip_b = _project_radius(vb, tuple(float(v) * scale for v in compound_center), "x")
        tip_c = _project_radius(vc, tuple(float(v) * scale for v in output_center), "y")

        margin = float(args.mesh_collision_margin)
        if str(args.drive_mode) == "motor":
            _setup_active_rigidbody(
                gear_a,
                mass=0.03,
                friction=float(args.rb_friction),
                linear_damping=float(args.rb_linear_damping),
                angular_damping=float(args.rb_angular_damping),
                collision_margin=margin,
            )
            _add_hinge(
                "InputGearHinge",
                "z",
                axle_a,
                gear_a,
                tuple(float(v) * scale for v in input_center),
                int(max(160, int(args.rb_iterations) * 4)),
            )
        else:
            _setup_passive_mesh_rigidbody(gear_a, friction=float(args.rb_friction), collision_margin=margin)
        _setup_active_rigidbody(
            gear_b,
            mass=float(args.gear_b_mass_kg),
            friction=float(args.rb_friction),
            linear_damping=float(args.rb_linear_damping),
            angular_damping=float(args.rb_angular_damping),
            collision_margin=margin,
        )
        _setup_active_rigidbody(
            gear_c,
            mass=float(args.gear_c_mass_kg),
            friction=float(args.rb_friction),
            linear_damping=float(args.rb_linear_damping),
            angular_damping=float(args.rb_angular_damping),
            collision_margin=margin,
        )
        _add_hinge(
            "CompoundGearHinge",
            "x",
            axle_b,
            gear_b,
            tuple(float(v) * scale for v in compound_center),
            int(max(160, int(args.rb_iterations) * 4)),
        )
        _add_hinge(
            "OutputGearHinge",
            "y",
            axle_c,
            gear_c,
            tuple(float(v) * scale for v in output_center),
            int(max(160, int(args.rb_iterations) * 4)),
        )

        pointer_a = _add_rotation_pointer("GearA_Pointer", gear_a, "z", tip_a, (0.20, 0.78, 0.95, 1.0))
        pointer_b = _add_rotation_pointer("GearB_Pointer", gear_b, "x", tip_b, (0.98, 0.62, 0.24, 1.0))
        pointer_c = _add_rotation_pointer("GearC_Pointer", gear_c, "y", tip_c, (0.32, 0.88, 0.42, 1.0))

        rpm_scale = max(0.01, float(args.drive_rpm_scale))
        omega_in = 2.0 * math.pi * float(args.input_rpm) * rpm_scale / 60.0
        if str(args.drive_mode) == "motor":
            _add_motor(
                "InputGearMotor",
                axle_a,
                gear_a,
                tuple(float(v) * scale for v in input_center),
                omega_in,
                float(args.motor_max_impulse),
                int(max(200, int(args.rb_iterations) * 5)),
            )
        else:
            gear_a.rotation_mode = "XYZ"
            axle_a.rotation_mode = "XYZ"
            phase_a = math.radians(float(args.phase_a_deg))
            for frame in range(scene.frame_start, scene.frame_end + 1):
                t = float(frame - scene.frame_start) / float(scene.render.fps)
                angle = phase_a + omega_in * t
                gear_a.rotation_euler = (0.0, 0.0, angle)
                gear_a.keyframe_insert(data_path="rotation_euler", frame=frame, index=2)
                axle_a.rotation_euler = (0.0, 0.0, angle)
                axle_a.keyframe_insert(data_path="rotation_euler", frame=frame, index=2)

        scene.frame_set(scene.frame_start)
        bpy.ops.ptcache.bake_all(bake=True)

        angles_a: list[float] = []
        angles_b: list[float] = []
        angles_c: list[float] = []
        for frame in range(scene.frame_start, scene.frame_end + 1):
            scene.frame_set(frame)
            angles_a.append(_pointer_angle(pointer_a, gear_a, "z"))
            angles_b.append(_pointer_angle(pointer_b, gear_b, "x"))
            angles_c.append(_pointer_angle(pointer_c, gear_c, "y"))

        input_rpm_raw = _fit_rpm(angles_a, scene.render.fps)
        mid_rpm_raw = _fit_rpm(angles_b, scene.render.fps)
        output_rpm_raw = _fit_rpm(angles_c, scene.render.fps)
        input_rpm = float(args.input_rpm)
        target_output_rpm = float(args.target_output_rpm)
        target_ratio = target_output_rpm / input_rpm if abs(input_rpm) > 1e-9 else 0.0
        mid_rpm = mid_rpm_raw / rpm_scale
        output_rpm = output_rpm_raw / rpm_scale
        input_rpm_measured = input_rpm_raw / rpm_scale
        out_ratio = output_rpm / input_rpm if abs(input_rpm) > 1e-9 else 0.0

        out_inst = _instant_rpms(angles_c, scene.render.fps)
        stop_fraction = float(np.mean(np.abs(np.asarray(out_inst, dtype=float)) <= 0.5)) if out_inst else 1.0
        in_win = _window_rpms(angles_a, scene.render.fps)
        out_win = _window_rpms(angles_c, scene.render.fps)
        tail_start = int(0.55 * min(len(in_win), len(out_win)))
        stop_events = 0
        active_events = 0
        for rpm_in_raw, rpm_out_raw in zip(in_win[tail_start:], out_win[tail_start:]):
            rpm_in = float(rpm_in_raw) / rpm_scale
            rpm_out = float(rpm_out_raw) / rpm_scale
            if abs(rpm_in) >= 20.0:
                active_events += 1
                if abs(rpm_out) < 2.0:
                    stop_events += 1
        window_stop_fraction = (float(stop_events) / float(active_events)) if active_events > 0 else 1.0

        direction_score = 1.0 if output_rpm * target_output_rpm > 0.0 else 0.0
        speed_score = _score_near(output_rpm, target_output_rpm, max(4.0, 0.12 * abs(target_output_rpm)))
        ratio_score = _score_near(out_ratio, target_ratio, 0.12) if abs(target_ratio) > 1e-9 else 0.0
        engaged = 1.0 if abs(output_rpm) > 2.0 and window_stop_fraction <= 0.35 else 0.0

        out.update(
            {
                "input_rpm_measured": float(input_rpm_measured),
                "mid_rpm": float(mid_rpm),
                "output_rpm": float(output_rpm),
                "direction_score": float(direction_score),
                "speed_score": float(speed_score),
                "ratio_score": float(ratio_score),
                "engaged": float(engaged),
                "stop_fraction": float(stop_fraction),
                "window_stop_fraction": float(window_stop_fraction),
                "frames": float(scene.frame_end),
            }
        )

        render_mp4 = (args.render_mp4 or "").strip()
        if render_mp4:
            output_y_mid = 0.5 * (float(args.output_axle_y_min_mm) + float(args.output_axle_y_max_mm))
            input_z_mid = 0.5 * (float(args.input_axle_z_min_mm) + float(args.input_axle_z_max_mm))
            center = Vector(
                (
                    0.5 * (float(args.input_axle_x_mm) + float(args.output_axle_x_mm)) * scale,
                    0.5 * (float(args.input_axle_y_mm) + output_y_mid) * scale,
                    0.5 * (input_z_mid + float(args.output_axle_z_mm)) * scale,
                )
            )
            span = max(
                0.12,
                1.15 * abs(float(args.output_axle_x_mm) - float(args.input_axle_x_mm)) * scale,
                1.10 * abs(float(args.compound_axle_x_max_mm) - float(args.compound_axle_x_min_mm)) * scale,
                2.4 * abs(float(args.output_axle_y_max_mm) - float(args.output_axle_y_min_mm)) * scale,
                2.2 * abs(float(args.input_axle_z_max_mm) - float(args.input_axle_z_min_mm)) * scale,
            )
            cameras: list[bpy.types.Object] = []
            for loc in (
                (center.x, center.y, center.z + 6.0 * span),
                (center.x + 4.0 * span, center.y - 4.8 * span, center.z + 3.2 * span),
            ):
                bpy.ops.object.camera_add(location=loc)
                cam = bpy.context.active_object
                _look_at(cam, center)
                cam.data.type = "ORTHO"
                cam.data.ortho_scale = 3.2 * span
                cameras.append(cam)
            _render_multicam_mp4(scene, cameras, render_mp4, float(args.render_slowdown), (0.93, 0.93, 0.93, 1.0))
    out_json.write_text(json.dumps(out, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
