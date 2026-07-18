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
    add_d_shaft_axle as _add_axle,
    add_hinge as _add_hinge,
    add_motor as _add_motor,
    add_rotation_pointer as _add_rotation_pointer,
    fit_rpm as _fit_rpm,
    import_stl as _import_stl,
    instant_rpms as _instant_rpms,
    look_at as _look_at,
    maybe_decimate_mesh as _maybe_decimate_mesh,
    maybe_voxel_remesh as _maybe_voxel_remesh,
    parse_blender_args as _parse_blender_args,
    render_multicam_mp4 as _render_multicam_mp4,
    score_near as _score_near,
    set_mesh_origin_on_axle as _set_mesh_origin_on_axle,
    setup_active_rigidbody as _setup_active_rigidbody,
    setup_passive_mesh_rigidbody as _setup_passive_mesh_rigidbody,
    spin_sign_about_world_z as _spin_sign_about_world_z,
    window_rpms as _window_rpms,
    yaw_from_world_matrix as _yaw_from_world_matrix,
)

def _parse_args() -> argparse.Namespace:
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = argv[1:]
    parser = argparse.ArgumentParser(description="Headless Blender rigid-body compound gearbox simulation")
    parser.add_argument("--gear-a-stl", required=True)
    parser.add_argument("--gear-b-stl", required=True)
    parser.add_argument("--gear-c-stl", required=True)
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--render-mp4", default="")
    parser.add_argument("--input-rpm", type=float, required=True)
    parser.add_argument("--target-output-rpm", type=float, required=True)
    parser.add_argument("--axle-a-x-mm", type=float, required=True)
    parser.add_argument("--axle-a-y-mm", type=float, required=True)
    parser.add_argument("--axle-b-x-mm", type=float, required=True)
    parser.add_argument("--axle-b-y-mm", type=float, required=True)
    parser.add_argument("--axle-c-x-mm", type=float, required=True)
    parser.add_argument("--axle-c-y-mm", type=float, required=True)
    parser.add_argument("--axle-radius-mm", type=float, required=True)
    parser.add_argument("--axle-flat-x-from-center-mm", type=float, default=0.0)
    parser.add_argument("--axle-z-min-mm", type=float, required=True)
    parser.add_argument("--axle-z-max-mm", type=float, required=True)
    parser.add_argument("--max-sim-faces", type=int, default=3600)
    parser.add_argument("--voxel-remesh-mm", type=float, default=0.0)
    parser.add_argument("--phase-a-deg", type=float, default=0.0)
    parser.add_argument("--phase-b-deg", type=float, default=0.0)
    parser.add_argument("--phase-c-deg", type=float, default=0.0)
    parser.add_argument("--gear-a-mass-kg", type=float, default=0.03)
    parser.add_argument("--gear-b-mass-kg", type=float, default=0.12)
    parser.add_argument("--gear-c-mass-kg", type=float, default=0.03)
    parser.add_argument("--rb-friction", type=float, default=0.75)
    parser.add_argument("--rb-linear-damping", type=float, default=0.08)
    parser.add_argument("--rb-angular-damping", type=float, default=0.05)
    parser.add_argument("--sim-fps", type=int, default=60)
    parser.add_argument("--sim-seconds", type=float, default=2.4)
    parser.add_argument("--rb-substeps", type=int, default=6)
    parser.add_argument("--rb-iterations", type=int, default=90)
    parser.add_argument("--drive-rpm-scale", type=float, default=0.25)
    parser.add_argument("--drive-mode", choices=["keyframe", "motor"], default="keyframe")
    parser.add_argument("--motor-max-impulse", type=float, default=0.18)
    parser.add_argument("--mesh-collision-margin", type=float, default=0.0004)
    parser.add_argument("--render-slowdown", type=float, default=2.5)
    return _parse_blender_args(parser)

def main() -> None:
    args = _parse_args()
    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out: dict[str, float | str] = {
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
        pc = scene.rigidbody_world.point_cache
        pc.frame_start = int(scene.frame_start)
        pc.frame_end = int(scene.frame_end)

        # A larger world scale is materially more stable for Blender's rigid-body
        # mesh contact than simulating tiny 20-50 mm gears directly at 1e-3.
        scale = 0.01
        z_mid_m = 0.5 * (args.axle_z_min_mm + args.axle_z_max_mm) * scale
        axle_a = _add_axle("AxleA", args.axle_a_x_mm, args.axle_a_y_mm, args.axle_radius_mm, args.axle_flat_x_from_center_mm, args.axle_z_min_mm, args.axle_z_max_mm, scale)
        axle_b = _add_axle("AxleB", args.axle_b_x_mm, args.axle_b_y_mm, args.axle_radius_mm, args.axle_flat_x_from_center_mm, args.axle_z_min_mm, args.axle_z_max_mm, scale)
        axle_c = _add_axle("AxleC", args.axle_c_x_mm, args.axle_c_y_mm, args.axle_radius_mm, args.axle_flat_x_from_center_mm, args.axle_z_min_mm, args.axle_z_max_mm, scale)

        gear_a = _import_stl(Path(args.gear_a_stl), "GearA", scale)
        gear_b = _import_stl(Path(args.gear_b_stl), "GearB", scale)
        gear_c = _import_stl(Path(args.gear_c_stl), "GearC", scale)
        for obj in (gear_a, gear_b, gear_c):
            _maybe_voxel_remesh(obj, float(args.voxel_remesh_mm), scale)
            _maybe_decimate_mesh(obj, int(args.max_sim_faces))

        _set_mesh_origin_on_axle(gear_a, args.axle_a_x_mm * scale, args.axle_a_y_mm * scale, z_mid_m)
        _set_mesh_origin_on_axle(gear_b, args.axle_b_x_mm * scale, args.axle_b_y_mm * scale, z_mid_m)
        _set_mesh_origin_on_axle(gear_c, args.axle_c_x_mm * scale, args.axle_c_y_mm * scale, z_mid_m)
        gear_a.rotation_euler = (0.0, 0.0, math.radians(float(args.phase_a_deg)))
        gear_b.rotation_euler = (0.0, 0.0, math.radians(float(args.phase_b_deg)))
        gear_c.rotation_euler = (0.0, 0.0, math.radians(float(args.phase_c_deg)))
        bpy.context.view_layer.update()

        va = np.asarray([(gear_a.matrix_world @ v.co)[:] for v in gear_a.data.vertices], dtype=float)
        vb = np.asarray([(gear_b.matrix_world @ v.co)[:] for v in gear_b.data.vertices], dtype=float)
        vc = np.asarray([(gear_c.matrix_world @ v.co)[:] for v in gear_c.data.vertices], dtype=float)
        tip_a = float(np.quantile(np.linalg.norm(va[:, :2] - np.array([args.axle_a_x_mm * scale, args.axle_a_y_mm * scale]), axis=1), 0.995))
        tip_b = float(np.quantile(np.linalg.norm(vb[:, :2] - np.array([args.axle_b_x_mm * scale, args.axle_b_y_mm * scale]), axis=1), 0.995))
        tip_c = float(np.quantile(np.linalg.norm(vc[:, :2] - np.array([args.axle_c_x_mm * scale, args.axle_c_y_mm * scale]), axis=1), 0.995))

        mesh_collision_margin = float(args.mesh_collision_margin)
        if str(args.drive_mode) == "motor":
            _setup_active_rigidbody(gear_a, mass=float(args.gear_a_mass_kg), friction=float(args.rb_friction), linear_damping=float(args.rb_linear_damping), angular_damping=float(args.rb_angular_damping), collision_margin=mesh_collision_margin)
        else:
            _setup_passive_mesh_rigidbody(gear_a, friction=float(args.rb_friction), collision_margin=mesh_collision_margin)
        _setup_active_rigidbody(gear_b, mass=float(args.gear_b_mass_kg), friction=float(args.rb_friction), linear_damping=float(args.rb_linear_damping), angular_damping=float(args.rb_angular_damping), collision_margin=mesh_collision_margin)
        _setup_active_rigidbody(gear_c, mass=float(args.gear_c_mass_kg), friction=float(args.rb_friction), linear_damping=float(args.rb_linear_damping), angular_damping=float(args.rb_angular_damping), collision_margin=mesh_collision_margin)
        if str(args.drive_mode) == "motor":
            _add_hinge("InputGearHinge", axle_a, gear_a, (args.axle_a_x_mm * scale, args.axle_a_y_mm * scale, z_mid_m), int(max(160, int(args.rb_iterations) * 4)))
        _add_hinge("CompoundGearHinge", axle_b, gear_b, (args.axle_b_x_mm * scale, args.axle_b_y_mm * scale, z_mid_m), int(max(160, int(args.rb_iterations) * 4)))
        _add_hinge("OutputGearHinge", axle_c, gear_c, (args.axle_c_x_mm * scale, args.axle_c_y_mm * scale, z_mid_m), int(max(160, int(args.rb_iterations) * 4)))

        _add_rotation_pointer("GearA_Pointer", gear_a, tip_a, max(0.0012, float(np.max(va[:, 2]) - gear_a.location.z + 0.0012)), (0.20, 0.78, 0.95, 1.0))
        _add_rotation_pointer("GearB_Pointer", gear_b, tip_b, max(0.0012, float(np.max(vb[:, 2]) - gear_b.location.z + 0.0012)), (0.98, 0.62, 0.24, 1.0))
        _add_rotation_pointer("GearC_Pointer", gear_c, tip_c, max(0.0012, float(np.max(vc[:, 2]) - gear_c.location.z + 0.0012)), (0.32, 0.88, 0.42, 1.0))

        rpm_scale = max(0.01, float(args.drive_rpm_scale))
        omega_in = 2.0 * math.pi * float(args.input_rpm) * rpm_scale / 60.0
        if str(args.drive_mode) == "motor":
            _add_motor(
                "InputGearMotor",
                axle_a,
                gear_a,
                (args.axle_a_x_mm * scale, args.axle_a_y_mm * scale, z_mid_m),
                omega_in,
                float(args.motor_max_impulse),
                int(max(200, int(args.rb_iterations) * 5)),
            )
        else:
            gear_a.rotation_mode = "XYZ"
            axle_a.rotation_mode = "XYZ"
            phase_a = math.radians(float(args.phase_a_deg))
            for f in range(scene.frame_start, scene.frame_end + 1):
                t = float(f - scene.frame_start) / float(scene.render.fps)
                angle = phase_a + omega_in * t
                gear_a.rotation_euler = (0.0, 0.0, angle)
                gear_a.keyframe_insert(data_path="rotation_euler", frame=f, index=2)
                axle_a.rotation_euler = (0.0, 0.0, angle)
                axle_a.keyframe_insert(data_path="rotation_euler", frame=f, index=2)

        scene.frame_set(scene.frame_start)
        bpy.ops.ptcache.bake_all(bake=True)

        angles_a: list[float] = []
        angles_b: list[float] = []
        angles_c: list[float] = []
        for f in range(scene.frame_start, scene.frame_end + 1):
            scene.frame_set(f)
            angles_a.append(_spin_sign_about_world_z(gear_a) * _yaw_from_world_matrix(gear_a))
            angles_b.append(_spin_sign_about_world_z(gear_b) * _yaw_from_world_matrix(gear_b))
            angles_c.append(_spin_sign_about_world_z(gear_c) * _yaw_from_world_matrix(gear_c))

        input_rpm_measured_raw = _fit_rpm(angles_a, scene.render.fps)
        mid_rpm_raw = _fit_rpm(angles_b, scene.render.fps)
        output_rpm_raw = _fit_rpm(angles_c, scene.render.fps)
        in_win = _window_rpms(angles_a, scene.render.fps)
        out_win = _window_rpms(angles_c, scene.render.fps)
        tail_start = int(0.55 * min(len(in_win), len(out_win)))
        stop_events = 0
        active_events = 0
        for rin, rout in zip(in_win[tail_start:], out_win[tail_start:]):
            rin_eq = float(rin) / rpm_scale
            rout_eq = float(rout) / rpm_scale
            if abs(rin_eq) >= 20.0:
                active_events += 1
                if abs(rout_eq) < 2.0:
                    stop_events += 1
        window_stop_fraction = (float(stop_events) / float(active_events)) if active_events > 0 else 1.0
        out_inst = _instant_rpms(angles_c, scene.render.fps)
        stop_fraction = float(np.mean(np.abs(np.asarray(out_inst, dtype=float)) <= 0.5)) if out_inst else 1.0

        input_rpm = float(args.input_rpm)
        target_output_rpm = float(args.target_output_rpm)
        target_ratio = target_output_rpm / input_rpm if abs(input_rpm) > 1e-9 else 0.0
        output_rpm = output_rpm_raw / rpm_scale
        mid_rpm = mid_rpm_raw / rpm_scale
        input_rpm_measured = input_rpm_measured_raw / rpm_scale
        out_ratio = output_rpm / input_rpm if abs(input_rpm) > 1e-9 else 0.0

        direction_score = 1.0 if output_rpm * target_output_rpm > 0.0 else 0.0
        speed_score = _score_near(output_rpm, target_output_rpm, max(4.0, 0.12 * abs(target_output_rpm)))
        ratio_score = _score_near(out_ratio, target_ratio, 0.12) if abs(target_ratio) > 1e-9 else 0.0
        engaged = 1.0 if abs(mid_rpm) > 2.0 and abs(output_rpm) > 2.0 else 0.0

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
            center = Vector((((args.axle_a_x_mm + args.axle_c_x_mm) * 0.5) * scale, ((args.axle_a_y_mm + args.axle_c_y_mm) * 0.5) * scale, ((args.axle_z_min_mm + args.axle_z_max_mm) * 0.5) * scale))
            span = max(0.05, abs(args.axle_c_x_mm - args.axle_a_x_mm) * scale)
            cameras: list[bpy.types.Object] = []
            for loc in (
                (center.x + 1.1 * span, center.y - 1.8 * span, center.z + 1.4 * span),
                (center.x, center.y, center.z + 3.0 * span),
                (center.x, center.y - 2.6 * span, center.z + 0.45 * span),
            ):
                bpy.ops.object.camera_add(location=loc)
                cam = bpy.context.active_object
                _look_at(cam, center)
                cameras.append(cam)
            cameras[1].data.type = "ORTHO"
            cameras[1].data.ortho_scale = 2.6 * span
            _render_multicam_mp4(scene, cameras, render_mp4, float(args.render_slowdown), (0.93, 0.93, 0.93, 1.0))
    out_json.write_text(json.dumps(out, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
