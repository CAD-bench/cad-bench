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
    add_rotation_pointer as _add_rotation_pointer,
    build_d_shaft_mesh as _build_d_shaft_mesh,
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
    unwrap_angles as _unwrap_angles,
    window_rpms as _window_rpms,
    yaw_from_world_matrix as _yaw_from_world_matrix,
)

def _parse_args() -> argparse.Namespace:
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = argv[1:]
    parser = argparse.ArgumentParser(description="Headless Blender rigid-body gearbox simulation (contact only)")
    parser.add_argument("--gear-a-stl", required=True)
    parser.add_argument("--gear-b-stl", required=True)
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--render-mp4", default="")
    parser.add_argument("--input-rpm", type=float, required=True)
    parser.add_argument("--target-output-rpm", type=float, required=True)
    parser.add_argument("--axle-a-x-mm", type=float, required=True)
    parser.add_argument("--axle-a-y-mm", type=float, required=True)
    parser.add_argument("--axle-b-x-mm", type=float, required=True)
    parser.add_argument("--axle-b-y-mm", type=float, required=True)
    parser.add_argument("--axle-radius-mm", type=float, required=True)
    parser.add_argument("--axle-flat-x-from-center-mm", type=float, default=0.0)
    parser.add_argument("--axle-z-min-mm", type=float, required=True)
    parser.add_argument("--axle-z-max-mm", type=float, required=True)
    parser.add_argument("--max-sim-faces", type=int, default=3200)
    parser.add_argument("--voxel-remesh-mm", type=float, default=0.0)
    parser.add_argument("--phase-a-deg", type=float, default=0.0)
    parser.add_argument("--phase-b-deg", type=float, default=0.0)
    parser.add_argument("--flip-gear-b-x", action="store_true")
    parser.add_argument("--gear-a-mass-kg", type=float, default=0.08)
    parser.add_argument("--gear-b-mass-kg", type=float, default=0.05)
    parser.add_argument("--rb-friction", type=float, default=0.8)
    parser.add_argument("--rb-linear-damping", type=float, default=0.08)
    parser.add_argument("--rb-angular-damping", type=float, default=0.04)
    parser.add_argument("--sim-fps", type=int, default=60)
    parser.add_argument("--sim-seconds", type=float, default=2.0)
    parser.add_argument("--rb-substeps", type=int, default=6)
    parser.add_argument("--rb-iterations", type=int, default=80)
    parser.add_argument("--drive-rpm-scale", type=float, default=0.25)
    parser.add_argument("--render-slowdown", type=float, default=2.5)
    return _parse_blender_args(parser)



def _add_axle_visual_cap(
    name: str,
    x_mm: float,
    y_mm: float,
    radius_mm: float,
    flat_x_from_center_mm: float,
    z_max_mm: float,
    scale: float,
) -> bpy.types.Object:
    # Purely visual marker so axle locations are visible in renders.
    cap_h_mm = 2.0
    cap_z_mid_mm = z_max_mm + 0.5 * cap_h_mm + 0.4
    obj = _build_d_shaft_mesh(
        name=name,
        x_mm=x_mm,
        y_mm=y_mm,
        radius_mm=max(0.4, radius_mm * 0.9),
        depth_mm=cap_h_mm,
        z_mid_mm=cap_z_mid_mm,
        flat_x_from_center_mm=flat_x_from_center_mm,
        scale=scale,
    )
    obj.color = (0.90, 0.35, 0.25, 1.0)
    return obj



def _direction_stats(angles: list[float], fps: int) -> dict[str, float]:
    u = _unwrap_angles(angles)
    if len(u) < 3:
        return {
            "mean_rpm": 0.0,
            "tail_mean_rpm": 0.0,
            "positive_frac": 0.0,
            "negative_frac": 0.0,
            "near_zero_frac": 1.0,
            "sign_flips": 0.0,
        }
    deltas = np.diff(np.asarray(u, dtype=float))
    rpms = deltas * float(fps) * 60.0 / (2.0 * math.pi)
    pos = float(np.mean(rpms > 0.5))
    neg = float(np.mean(rpms < -0.5))
    zero = float(np.mean(np.abs(rpms) <= 0.5))
    signs = np.sign(rpms)
    nz = signs[np.abs(rpms) > 0.5]
    flips = 0
    if len(nz) >= 2:
        flips = int(np.sum(nz[1:] != nz[:-1]))
    tail_i = int(max(0, 0.60 * len(rpms)))
    tail = rpms[tail_i:] if tail_i < len(rpms) else rpms
    return {
        "mean_rpm": float(np.mean(rpms)),
        "tail_mean_rpm": float(np.mean(tail)) if len(tail) else 0.0,
        "positive_frac": pos,
        "negative_frac": neg,
        "near_zero_frac": zero,
        "sign_flips": float(flips),
    }


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
        "frames": 0.0,
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

        scale = 0.001
        axle_a = _add_axle(
            "AxleA",
            args.axle_a_x_mm,
            args.axle_a_y_mm,
            args.axle_radius_mm,
            args.axle_flat_x_from_center_mm,
            args.axle_z_min_mm,
            args.axle_z_max_mm,
            scale,
        )
        axle_b = _add_axle(
            "AxleB",
            args.axle_b_x_mm,
            args.axle_b_y_mm,
            args.axle_radius_mm,
            args.axle_flat_x_from_center_mm,
            args.axle_z_min_mm,
            args.axle_z_max_mm,
            scale,
        )

        gear_a_vis = _import_stl(Path(args.gear_a_stl), "GearA_Visual", scale=scale)
        gear_b_vis = _import_stl(Path(args.gear_b_stl), "GearB_Visual", scale=scale)
        _maybe_voxel_remesh(gear_a_vis, float(args.voxel_remesh_mm), scale)
        _maybe_voxel_remesh(gear_b_vis, float(args.voxel_remesh_mm), scale)
        _maybe_decimate_mesh(gear_a_vis, int(args.max_sim_faces))
        _maybe_decimate_mesh(gear_b_vis, int(args.max_sim_faces))
        gear_a_vis.color = (0.73, 0.78, 0.86, 1.0)
        gear_b_vis.color = (0.88, 0.88, 0.92, 1.0)

        z_mid_m = ((args.axle_z_min_mm + args.axle_z_max_mm) * 0.5) * scale
        # Pure simulation path: use only the two imported gear meshes as rigid bodies.
        gear_a = gear_a_vis
        gear_b = gear_b_vis
        _set_mesh_origin_on_axle(gear_a, args.axle_a_x_mm * scale, args.axle_a_y_mm * scale, z_mid_m)
        _set_mesh_origin_on_axle(gear_b, args.axle_b_x_mm * scale, args.axle_b_y_mm * scale, z_mid_m)
        phase_a = math.radians(float(args.phase_a_deg))
        phase_b = math.radians(float(args.phase_b_deg))
        gear_a.rotation_euler = (0.0, 0.0, phase_a)
        gear_b.rotation_euler = (math.pi if bool(args.flip_gear_b_x) else 0.0, 0.0, phase_b)
        bpy.context.view_layer.update()

        # Estimate tip radii from geometry for visual pointer placement.
        va = np.asarray([(gear_a.matrix_world @ v.co)[:] for v in gear_a.data.vertices], dtype=float)
        vb = np.asarray([(gear_b.matrix_world @ v.co)[:] for v in gear_b.data.vertices], dtype=float)
        face_count_a = int(len(gear_a.data.polygons))
        face_count_b = int(len(gear_b.data.polygons))
        cz_b = float(np.mean(vb[:, 2])) if len(vb) else z_mid_m
        tip_a_m = float(
            np.quantile(
                np.linalg.norm(va[:, :2] - np.array([args.axle_a_x_mm * scale, args.axle_a_y_mm * scale]), axis=1),
                0.995,
            )
        )
        tip_b_m = float(
            np.quantile(
                np.linalg.norm(vb[:, :2] - np.array([args.axle_b_x_mm * scale, args.axle_b_y_mm * scale]), axis=1),
                0.995,
            )
        )

        _setup_passive_mesh_rigidbody(gear_a)
        if gear_a.rigid_body is not None:
            gear_a.rigid_body.friction = max(0.0, float(args.rb_friction))
        _setup_active_rigidbody(
            gear_b,
            mass=float(args.gear_b_mass_kg),
            collision_shape="MESH",
            friction=float(args.rb_friction),
            linear_damping=float(args.rb_linear_damping),
            angular_damping=float(args.rb_angular_damping),
        )

        _add_axle_visual_cap(
            "AxleA_VisCap",
            args.axle_a_x_mm,
            args.axle_a_y_mm,
            args.axle_radius_mm,
            args.axle_flat_x_from_center_mm,
            args.axle_z_max_mm,
            scale,
        )
        _add_axle_visual_cap(
            "AxleB_VisCap",
            args.axle_b_x_mm,
            args.axle_b_y_mm,
            args.axle_radius_mm,
            args.axle_flat_x_from_center_mm,
            args.axle_z_max_mm,
            scale,
        )

        # Asymmetric visual markers make rotation obvious in sampled frames.
        pointer_a = _add_rotation_pointer(
            "GearA_RotationPointer",
            parent_obj=gear_a,
            tip_radius_m=tip_a_m,
            z_offset_m=max(0.0012, float(np.max(va[:, 2]) - gear_a.location.z + 0.0012)),
            color=(0.20, 0.78, 0.95, 1.0),
        )
        pointer_b = _add_rotation_pointer(
            "GearB_RotationPointer",
            parent_obj=gear_b,
            tip_radius_m=tip_b_m,
            z_offset_m=max(0.0012, float(np.max(vb[:, 2]) - gear_b.location.z + 0.0012)),
            color=(0.98, 0.82, 0.24, 1.0),
        )

        rpm_scale = max(0.01, float(args.drive_rpm_scale))
        omega_in = 2.0 * math.pi * float(args.input_rpm) * rpm_scale / 60.0
        # Pure Blender setup:
        # - Input gear is an animated passive rigid body.
        # - Output gear is an active rigid body constrained by a hinge.
        _add_hinge(
            "OutputGearHinge",
            axle_obj=axle_b,
            gear_obj=gear_b,
            loc_m=(args.axle_b_x_mm * scale, args.axle_b_y_mm * scale, cz_b),
            solver_iterations=int(max(80, int(args.rb_iterations) * 3)),
        )

        gear_a.rotation_mode = "XYZ"
        axle_a.rotation_mode = "XYZ"
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
        pointer_angles_a: list[float] = []
        pointer_angles_b: list[float] = []
        b_euler_start = (0.0, 0.0, 0.0)
        b_euler_end = (0.0, 0.0, 0.0)
        b_loc_start = (0.0, 0.0, 0.0)
        b_loc_end = (0.0, 0.0, 0.0)
        for f in range(scene.frame_start, scene.frame_end + 1):
            scene.frame_set(f)
            sign_a = _spin_sign_about_world_z(gear_a)
            sign_b = _spin_sign_about_world_z(gear_b)
            ang_a = sign_a * _yaw_from_world_matrix(gear_a)
            ang_b = sign_b * _yaw_from_world_matrix(gear_b)
            angles_a.append(ang_a)
            angles_b.append(ang_b)
            pointer_angles_a.append(sign_a * _yaw_from_world_matrix(pointer_a))
            pointer_angles_b.append(sign_b * _yaw_from_world_matrix(pointer_b))
            e = gear_b.matrix_world.to_euler()
            loc = gear_b.matrix_world.translation
            if f == scene.frame_start:
                b_euler_start = (float(e.x), float(e.y), float(e.z))
                b_loc_start = (float(loc.x), float(loc.y), float(loc.z))
            if f == scene.frame_end:
                b_euler_end = (float(e.x), float(e.y), float(e.z))
                b_loc_end = (float(loc.x), float(loc.y), float(loc.z))

        input_rpm_measured_raw = _fit_rpm(angles_a, scene.render.fps)
        output_rpm_raw = _fit_rpm(angles_b, scene.render.fps)
        in_win = _window_rpms(angles_a, scene.render.fps)
        out_win = _window_rpms(angles_b, scene.render.fps)
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

        input_rpm = float(args.input_rpm)
        target_output_rpm = float(args.target_output_rpm)
        target_ratio = target_output_rpm / input_rpm if abs(input_rpm) > 1e-9 else 0.0
        input_rpm_measured = input_rpm_measured_raw / rpm_scale
        output_rpm = output_rpm_raw / rpm_scale
        out_ratio = output_rpm / input_rpm if abs(input_rpm) > 1e-9 else 0.0

        speed_tol = max(4.0, 0.12 * abs(target_output_rpm))
        ratio_tol = 0.12
        direction_score = 1.0 if output_rpm * target_output_rpm > 0.0 else 0.0
        speed_score = _score_near(output_rpm, target_output_rpm, speed_tol)
        ratio_score = _score_near(out_ratio, target_ratio, ratio_tol) if abs(target_ratio) > 1e-9 else 0.0
        engaged = 1.0 if abs(output_rpm) > 2.0 else 0.0
        pointer_a_stats = _direction_stats(pointer_angles_a, scene.render.fps)
        pointer_b_stats = _direction_stats(pointer_angles_b, scene.render.fps)
        pointer_rpms_a = _instant_rpms(pointer_angles_a, scene.render.fps)
        pointer_rpms_b = _instant_rpms(pointer_angles_b, scene.render.fps)
        pointer_b_stop_fraction = (
            float(np.mean(np.abs(np.asarray(pointer_rpms_b, dtype=float)) <= 0.5))
            if pointer_rpms_b
            else 1.0
        )

        stop_fraction = pointer_b_stop_fraction

        out.update(
            {
                "output_rpm": float(output_rpm),
                "output_rpm_raw": float(output_rpm_raw),
                "drive_rpm_scale": float(rpm_scale),
                "phase_a_deg": float(args.phase_a_deg),
                "phase_b_deg": float(args.phase_b_deg),
                "flip_gear_b_x": 1.0 if bool(args.flip_gear_b_x) else 0.0,
                "gear_a_mass_kg": float(args.gear_a_mass_kg),
                "gear_b_mass_kg": float(args.gear_b_mass_kg),
                "rb_friction": float(args.rb_friction),
                "rb_linear_damping": float(args.rb_linear_damping),
                "rb_angular_damping": float(args.rb_angular_damping),
                "input_rpm_measured": float(input_rpm_measured),
                "input_rpm_measured_raw": float(input_rpm_measured_raw),
                "output_rpm_x": 0.0,
                "output_rpm_y": 0.0,
                "output_rpm_z": float(output_rpm_raw),
                "debug_b_euler_start_x": float(b_euler_start[0]),
                "debug_b_euler_end_x": float(b_euler_end[0]),
                "debug_b_euler_start_y": float(b_euler_start[1]),
                "debug_b_euler_end_y": float(b_euler_end[1]),
                "debug_b_euler_start_z": float(b_euler_start[2]),
                "debug_b_euler_end_z": float(b_euler_end[2]),
                "debug_b_loc_start_x": float(b_loc_start[0]),
                "debug_b_loc_end_x": float(b_loc_end[0]),
                "stop_fraction": float(stop_fraction),
                "window_stop_fraction": float(window_stop_fraction),
                "direction_score": float(direction_score),
                "speed_score": float(speed_score),
                "ratio_score": float(ratio_score),
                "engaged": float(engaged),
                "frames": float(scene.frame_end),
                "sim_faces_a": float(face_count_a),
                "sim_faces_b": float(face_count_b),
                "voxel_remesh_mm": float(args.voxel_remesh_mm),
                "pointer_a_mean_rpm": float(pointer_a_stats["mean_rpm"]),
                "pointer_a_tail_mean_rpm": float(pointer_a_stats["tail_mean_rpm"]),
                "pointer_a_mean_rpm_equiv": float(pointer_a_stats["mean_rpm"] / rpm_scale),
                "pointer_a_tail_mean_rpm_equiv": float(pointer_a_stats["tail_mean_rpm"] / rpm_scale),
                "pointer_a_positive_frac": float(pointer_a_stats["positive_frac"]),
                "pointer_a_negative_frac": float(pointer_a_stats["negative_frac"]),
                "pointer_a_near_zero_frac": float(pointer_a_stats["near_zero_frac"]),
                "pointer_a_sign_flips": float(pointer_a_stats["sign_flips"]),
                "pointer_b_mean_rpm": float(pointer_b_stats["mean_rpm"]),
                "pointer_b_tail_mean_rpm": float(pointer_b_stats["tail_mean_rpm"]),
                "pointer_b_mean_rpm_equiv": float(pointer_b_stats["mean_rpm"] / rpm_scale),
                "pointer_b_tail_mean_rpm_equiv": float(pointer_b_stats["tail_mean_rpm"] / rpm_scale),
                "pointer_b_positive_frac": float(pointer_b_stats["positive_frac"]),
                "pointer_b_negative_frac": float(pointer_b_stats["negative_frac"]),
                "pointer_b_near_zero_frac": float(pointer_b_stats["near_zero_frac"]),
                "pointer_b_sign_flips": float(pointer_b_stats["sign_flips"]),
                "pointer_a_angles_sample": [float(a) for a in _unwrap_angles(pointer_angles_a)[:40]],
                "pointer_b_angles_sample": [float(a) for a in _unwrap_angles(pointer_angles_b)[:40]],
                "pointer_a_rpm_series": [float(v) for v in pointer_rpms_a],
                "pointer_b_rpm_series": [float(v) for v in pointer_rpms_b],
                "pointer_b_stop_fraction": float(pointer_b_stop_fraction),
            }
        )

        render_mp4 = (args.render_mp4 or "").strip()
        if render_mp4:
            center = Vector(
                (
                    (args.axle_a_x_mm + args.axle_b_x_mm) * 0.5 * scale,
                    (args.axle_a_y_mm + args.axle_b_y_mm) * 0.5 * scale,
                    (args.axle_z_min_mm + args.axle_z_max_mm) * 0.5 * scale,
                )
            )
            span = max(0.04, abs(args.axle_b_x_mm - args.axle_a_x_mm) * scale)
            focus = center + Vector((0.0, 0.0, 0.10 * span))
            cameras: list[bpy.types.Object] = []
            for loc in (
                Vector((center.x + 1.4 * span, center.y - 2.0 * span, center.z + 1.6 * span)),
                Vector((center.x, center.y, center.z + 3.0 * span)),
                Vector((center.x, center.y - 2.6 * span, center.z + 0.5 * span)),
            ):
                bpy.ops.object.camera_add(location=loc)
                cam = bpy.context.active_object
                _look_at(cam, focus)
                cameras.append(cam)
            cameras[0].data.type = "ORTHO"
            cameras[0].data.ortho_scale = 1.8 * span
            cameras[1].data.type = "ORTHO"
            cameras[1].data.ortho_scale = 1.55 * span
            scene.frame_start = 1
            scene.frame_end = int(scene.frame_end)
            _render_multicam_mp4(scene, cameras, render_mp4, float(args.render_slowdown), 1, (0.93, 0.93, 0.93, 1.0))

    out_json.write_text(json.dumps(out, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
