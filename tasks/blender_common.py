from __future__ import annotations

import argparse
import math
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import bpy
import numpy as np
from mathutils import Vector


def parse_blender_args(parser: argparse.ArgumentParser) -> argparse.Namespace:
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = argv[1:]
    return parser.parse_args(argv)


def axis_basis(axis: str) -> tuple[Vector, Vector, Vector]:
    if axis == "x":
        return Vector((0.0, 1.0, 0.0)), Vector((0.0, 0.0, 1.0)), Vector((1.0, 0.0, 0.0))
    if axis == "y":
        return Vector((0.0, 0.0, 1.0)), Vector((1.0, 0.0, 0.0)), Vector((0.0, 1.0, 0.0))
    if axis == "z":
        return Vector((1.0, 0.0, 0.0)), Vector((0.0, 1.0, 0.0)), Vector((0.0, 0.0, 1.0))
    raise ValueError(f"unsupported axis: {axis}")


def axis_rotation(axis: str) -> tuple[float, float, float]:
    if axis == "x":
        return (0.0, 0.5 * math.pi, 0.0)
    if axis == "y":
        return (-0.5 * math.pi, 0.0, 0.0)
    if axis == "z":
        return (0.0, 0.0, 0.0)
    raise ValueError(f"unsupported axis: {axis}")


def phase_rotation(axis: str, deg: float) -> tuple[float, float, float]:
    rad = math.radians(float(deg))
    if axis == "x":
        return (rad, 0.0, 0.0)
    if axis == "y":
        return (0.0, rad, 0.0)
    if axis == "z":
        return (0.0, 0.0, rad)
    raise ValueError(f"unsupported axis: {axis}")


def look_at(obj: bpy.types.Object, target: Vector) -> None:
    quat = (target - obj.location).to_track_quat("-Z", "Y")
    obj.rotation_euler = quat.to_euler()


def render_multicam_mp4(
    scene: bpy.types.Scene,
    cameras: list[bpy.types.Object],
    render_mp4: str,
    slowdown: float,
    frame_step: int,
    bg_color: tuple[float, float, float, float],
) -> None:
    frame_step = int(frame_step)
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = 640
    scene.render.resolution_y = 480
    scene.render.image_settings.file_format = "PNG"
    scene.display.shading.color_type = "OBJECT"
    scene.display.shading.light = "STUDIO"
    scene.display.shading.show_cavity = False

    world = bpy.data.worlds.new("World")
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    bg.inputs[0].default_value = bg_color
    bg.inputs[1].default_value = 1.0

    with tempfile.TemporaryDirectory(prefix="blender_frames_") as tdir:
        frame_root = Path(tdir)
        input_args: list[str] = []
        filter_parts: list[str] = []
        sampled_fps = max(1.0, float(scene.render.fps) / float(max(1, frame_step)))
        for idx, cam in enumerate(cameras):
            cam_dir = frame_root / f"cam_{idx}"
            cam_dir.mkdir(parents=True, exist_ok=True)
            scene.camera = cam
            if frame_step <= 1:
                scene.render.filepath = (cam_dir / "frame_").as_posix()
                bpy.ops.render.render(animation=True, write_still=False)
                input_args.extend(["-i", (cam_dir / "frame_%04d.png").as_posix()])
            else:
                frame_idx = 0
                for frame in range(
                    int(scene.frame_start), int(scene.frame_end) + 1, max(1, frame_step)
                ):
                    scene.frame_set(frame)
                    scene.render.filepath = (
                        cam_dir / f"frame_{frame_idx:04d}.png"
                    ).as_posix()
                    bpy.ops.render.render(write_still=True)
                    frame_idx += 1
                input_args.extend(["-i", (cam_dir / "frame_%04d.png").as_posix()])
            filter_parts.append(
                f"[{idx}:v]setpts={max(1.0, float(slowdown)):.4f}*PTS[v{idx}]"
            )
        if len(cameras) == 1:
            filter_parts.append("[v0]copy[vout]")
        else:
            filter_parts.append(
                f"{''.join(f'[v{idx}]' for idx in range(len(cameras)))}hstack=inputs={len(cameras)}[vout]"
            )
        mp4_path = Path(render_mp4)
        mp4_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-framerate",
                f"{sampled_fps:.6f}",
                *input_args,
                "-filter_complex",
                ";".join(filter_parts),
                "-map",
                "[vout]",
                "-r",
                "24",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-crf",
                "18",
                mp4_path.as_posix(),
            ],
            capture_output=True,
            text=True,
            check=True,
        )


def import_stl(path: Path, name: str, scale: float) -> bpy.types.Object:
    before = set(bpy.data.objects.keys())
    bpy.ops.wm.stl_import(filepath=path.as_posix())
    created = [
        bpy.data.objects[key] for key in bpy.data.objects.keys() if key not in before
    ]
    if not created:
        raise RuntimeError(f"No object imported from STL: {path}")
    obj = created[0]
    obj.name = name
    obj.scale = (scale, scale, scale)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    return obj


def maybe_decimate_mesh(obj: bpy.types.Object, max_faces: int) -> None:
    max_faces = int(max_faces)
    if max_faces <= 0 or obj.type != "MESH" or obj.data is None:
        return
    face_count = int(len(obj.data.polygons))
    if face_count <= max_faces:
        return
    bpy.context.view_layer.objects.active = obj
    mod = obj.modifiers.new(name="RBDecimate", type="DECIMATE")
    mod.decimate_type = "COLLAPSE"
    mod.ratio = max(0.02, min(1.0, float(max_faces) / float(face_count)))
    mod.use_collapse_triangulate = True
    bpy.ops.object.modifier_apply(modifier=mod.name)


def maybe_voxel_remesh(obj: bpy.types.Object, voxel_mm: float, scale: float) -> None:
    if obj.type != "MESH" or obj.data is None or voxel_mm <= 0.0:
        return
    bpy.context.view_layer.objects.active = obj
    mod = obj.modifiers.new(name="RBRemesh", type="REMESH")
    mod.mode = "VOXEL"
    mod.voxel_size = max(0.00002, voxel_mm * scale)
    mod.adaptivity = 0.0
    mod.use_smooth_shade = False
    bpy.ops.object.modifier_apply(modifier=mod.name)


def build_d_shaft_mesh(
    name: str,
    x_mm: float,
    y_mm: float,
    radius_mm: float,
    depth_mm: float,
    z_mid_mm: float,
    flat_x_from_center_mm: float,
    scale: float,
) -> bpy.types.Object:
    radius_m = max(0.0002, radius_mm * scale)
    depth_m = max(0.0003, depth_mm * scale)
    flat_x_mm = min(max(-radius_mm + 0.05, flat_x_from_center_mm), radius_mm - 0.05)
    flat_x_m = flat_x_mm * scale
    cx = x_mm * scale
    cy = y_mm * scale
    cz = z_mid_mm * scale

    bpy.ops.mesh.primitive_cylinder_add(
        vertices=64, radius=radius_m, depth=depth_m, location=(cx, cy, cz)
    )
    shaft = bpy.context.active_object
    shaft.name = name

    bpy.ops.mesh.primitive_cube_add(
        size=1.0, location=(cx + flat_x_m + radius_m, cy, cz)
    )
    cutter = bpy.context.active_object
    cutter.scale = (2.0 * radius_m, 2.7 * radius_m, 1.5 * depth_m)
    bpy.context.view_layer.objects.active = cutter
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

    bpy.context.view_layer.objects.active = shaft
    mod = shaft.modifiers.new(name=f"{name}_DFlat", type="BOOLEAN")
    mod.operation = "DIFFERENCE"
    mod.solver = "EXACT"
    mod.object = cutter
    bpy.ops.object.modifier_apply(modifier=mod.name)
    bpy.data.objects.remove(cutter, do_unlink=True)
    return shaft


def add_d_shaft_axle(
    name: str,
    x_mm: float,
    y_mm: float,
    radius_mm: float,
    flat_x_from_center_mm: float,
    z_min_mm: float,
    z_max_mm: float,
    scale: float,
) -> bpy.types.Object:
    obj = build_d_shaft_mesh(
        name=name,
        x_mm=x_mm,
        y_mm=y_mm,
        radius_mm=radius_mm,
        depth_mm=max(0.1, z_max_mm - z_min_mm),
        z_mid_mm=0.5 * (z_min_mm + z_max_mm),
        flat_x_from_center_mm=flat_x_from_center_mm,
        scale=scale,
    )
    obj.color = (0.90, 0.35, 0.25, 1.0)
    bpy.ops.rigidbody.object_add()
    rb = obj.rigid_body
    rb.type = "PASSIVE"
    rb.collision_shape = "MESH"
    rb.mesh_source = "FINAL"
    rb.friction = 0.0
    rb.restitution = 0.0
    rb.use_margin = True
    rb.collision_margin = 0.00002
    return obj


def add_axle(
    name: str,
    axis: str,
    center_mm: tuple[float, float, float],
    length_mm: float,
    radius_mm: float,
    scale: float,
) -> bpy.types.Object:
    bpy.ops.mesh.primitive_cylinder_add(
        vertices=64,
        radius=max(0.0002, radius_mm * scale),
        depth=max(0.0003, length_mm * scale),
        location=tuple(float(v) * scale for v in center_mm),
        rotation=axis_rotation(axis),
    )
    obj = bpy.context.active_object
    obj.name = name
    obj.color = (0.90, 0.35, 0.25, 1.0)
    bpy.ops.rigidbody.object_add()
    rb = obj.rigid_body
    rb.type = "PASSIVE"
    rb.collision_shape = "MESH"
    rb.mesh_source = "FINAL"
    rb.friction = 0.0
    rb.restitution = 0.0
    rb.use_margin = True
    rb.collision_margin = 0.00002
    return obj


def setup_active_rigidbody(
    obj: bpy.types.Object,
    mass: float,
    friction: float = 0.8,
    linear_damping: float = 0.10,
    angular_damping: float = 0.05,
    collision_margin: float = 0.00002,
    collision_shape: str = "MESH",
) -> None:
    bpy.context.view_layer.objects.active = obj
    bpy.ops.rigidbody.object_add()
    rb = obj.rigid_body
    rb.type = "ACTIVE"
    rb.collision_shape = collision_shape
    if collision_shape == "MESH":
        rb.mesh_source = "FINAL"
    rb.mass = max(0.01, float(mass))
    rb.friction = max(0.0, float(friction))
    rb.linear_damping = max(0.0, float(linear_damping))
    rb.angular_damping = max(0.0, float(angular_damping))
    rb.restitution = 0.0
    rb.use_deactivation = False
    rb.use_margin = True
    rb.collision_margin = max(0.0, float(collision_margin))


def setup_passive_mesh_rigidbody(
    obj: bpy.types.Object, friction: float = 0.0, collision_margin: float = 0.00002
) -> None:
    bpy.context.view_layer.objects.active = obj
    bpy.ops.rigidbody.object_add()
    rb = obj.rigid_body
    rb.type = "PASSIVE"
    rb.collision_shape = "MESH"
    rb.mesh_source = "FINAL"
    rb.friction = max(0.0, float(friction))
    rb.restitution = 0.0
    rb.use_margin = True
    rb.collision_margin = max(0.0, float(collision_margin))
    if hasattr(rb, "kinematic"):
        rb.kinematic = True


def add_hinge(name: str, *args: Any, **kwargs: Any) -> bpy.types.Object:
    axis: str | None = None
    solver_iterations = int(kwargs.pop("solver_iterations", 400))
    if kwargs:
        axis = kwargs.pop("axis", None)
        obj_a = kwargs.pop("axle_obj")
        obj_b = kwargs.pop("gear_obj")
        location = kwargs.pop("loc_m")
        if kwargs:
            raise TypeError(f"unexpected hinge kwargs: {sorted(kwargs)}")
    elif len(args) == 4:
        obj_a, obj_b, location, solver_iterations = args
    elif len(args) == 5 and isinstance(args[0], str):
        axis, obj_a, obj_b, location, solver_iterations = args
    else:
        raise TypeError("unsupported add_hinge call shape")
    rotation = axis_rotation(axis) if axis is not None else (0.0, 0.0, 0.0)
    bpy.ops.object.empty_add(type="PLAIN_AXES", location=location, rotation=rotation)
    hinge = bpy.context.active_object
    hinge.name = name
    bpy.context.view_layer.objects.active = hinge
    bpy.ops.rigidbody.constraint_add()
    rbc = hinge.rigid_body_constraint
    rbc.type = "HINGE"
    rbc.object1 = obj_a
    rbc.object2 = obj_b
    rbc.disable_collisions = True
    if hasattr(rbc, "use_override_solver_iterations"):
        rbc.use_override_solver_iterations = True
    if hasattr(rbc, "solver_iterations"):
        rbc.solver_iterations = int(max(20, solver_iterations))
    for attr in (
        "use_limit_ang_x",
        "use_limit_ang_y",
        "use_limit_ang_z",
        "use_limit_lin_x",
        "use_limit_lin_y",
        "use_limit_lin_z",
    ):
        if hasattr(rbc, attr):
            setattr(rbc, attr, False)
    return hinge


def add_motor(name: str, *args: Any) -> bpy.types.Object:
    if len(args) != 6:
        raise TypeError("unsupported add_motor call shape")
    obj_a, obj_b, location, target_velocity, max_impulse, solver_iterations = args
    bpy.ops.object.empty_add(
        type="PLAIN_AXES", location=location, rotation=(0.0, -0.5 * math.pi, 0.0)
    )
    motor = bpy.context.active_object
    motor.name = name
    bpy.context.view_layer.objects.active = motor
    bpy.ops.rigidbody.constraint_add()
    rbc = motor.rigid_body_constraint
    rbc.type = "MOTOR"
    rbc.object1 = obj_a
    rbc.object2 = obj_b
    rbc.disable_collisions = True
    rbc.use_motor_ang = True
    rbc.motor_ang_target_velocity = float(target_velocity)
    rbc.motor_ang_max_impulse = max(0.001, float(max_impulse))
    if hasattr(rbc, "use_override_solver_iterations"):
        rbc.use_override_solver_iterations = True
    if hasattr(rbc, "solver_iterations"):
        rbc.solver_iterations = int(max(20, solver_iterations))
    return motor


def add_rotation_pointer(name: str, *args: Any, **kwargs: Any) -> bpy.types.Object:
    if kwargs:
        parent = kwargs["parent_obj"]
        tip_radius_m = float(kwargs["tip_radius_m"])
        z_offset_m = float(kwargs["z_offset_m"])
        color = kwargs["color"]
        pointer_len = max(0.002, tip_radius_m * 1.05)
        pointer_thick = max(0.0006, tip_radius_m * 0.08)
        location = (
            float(parent.location.x) + 0.5 * pointer_len,
            float(parent.location.y),
            float(parent.location.z) + z_offset_m,
        )
        rotation = (0.0, 0.0, 0.0)
        scale_xyz = (pointer_len, pointer_thick, pointer_thick)
    elif len(args) == 4 and isinstance(args[1], str):
        parent, axis, tip_radius_m, color = args
        e1, _, up = axis_basis(axis)
        pointer_len = max(0.002, float(tip_radius_m) * 1.05)
        pointer_thick = max(0.0005, float(tip_radius_m) * 0.08)
        loc = (
            parent.location + e1 * pointer_len + up * max(0.0012, 0.10 * pointer_thick)
        )
        location = (float(loc.x), float(loc.y), float(loc.z))
        rotation = (0.0, 0.0, 0.0)
        scale_xyz = (pointer_thick, pointer_thick, pointer_thick)
    elif len(args) == 4:
        parent, tip_radius_m, z_offset_m, color = args
        pointer_len = max(0.002, float(tip_radius_m) * 1.05)
        pointer_thick = max(0.0006, float(tip_radius_m) * 0.08)
        location = (
            float(parent.location.x) + 0.5 * pointer_len,
            float(parent.location.y),
            float(parent.location.z) + float(z_offset_m),
        )
        rotation = (0.0, 0.0, 0.0)
        scale_xyz = (pointer_len, pointer_thick, pointer_thick)
    else:
        raise TypeError("unsupported add_rotation_pointer call shape")
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=location, rotation=rotation)
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = scale_xyz
    obj.color = color
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    obj.parent = parent
    obj.matrix_parent_inverse = parent.matrix_world.inverted()
    return obj


def set_mesh_origin_on_axle(
    obj: bpy.types.Object, axle_x_m: float, axle_y_m: float, axle_z_mid_m: float
) -> None:
    offset = Vector((float(axle_x_m), float(axle_y_m), float(axle_z_mid_m)))
    for vert in obj.data.vertices:
        vert.co = vert.co - offset
    obj.data.update()
    obj.location = offset


def set_mesh_origin(
    obj: bpy.types.Object, origin_m: tuple[float, float, float]
) -> None:
    offset = Vector(origin_m)
    for vert in obj.data.vertices:
        vert.co = vert.co - offset
    obj.data.update()
    obj.location = offset


def yaw_from_world_matrix(obj: bpy.types.Object) -> float:
    axis = obj.matrix_world.to_3x3() @ Vector((1.0, 0.0, 0.0))
    return math.atan2(axis.y, axis.x)


def spin_sign_about_world_z(obj: bpy.types.Object) -> float:
    axis = obj.matrix_world.to_3x3() @ Vector((0.0, 0.0, 1.0))
    return -1.0 if axis.z < 0.0 else 1.0


def unwrap_angles(angles: list[float]) -> list[float]:
    if not angles:
        return []
    out = [angles[0]]
    for angle in angles[1:]:
        prev = out[-1]
        delta = angle - prev
        while delta <= -math.pi:
            angle += 2.0 * math.pi
            delta = angle - prev
        while delta > math.pi:
            angle -= 2.0 * math.pi
            delta = angle - prev
        out.append(angle)
    return out


def fit_rpm(angles: list[float], fps: int) -> float:
    if len(angles) < 10:
        return 0.0
    y = unwrap_angles(angles)
    start = int(len(y) * 0.60)
    end = len(y) - 1
    if end - start < 3:
        return 0.0
    x = [float(i - start) / float(max(1, fps)) for i in range(start, end + 1)]
    y_slice = y[start : end + 1]
    x_mean = sum(x) / len(x)
    y_mean = sum(y_slice) / len(y_slice)
    denom = sum((value - x_mean) * (value - x_mean) for value in x)
    if denom <= 1e-12:
        return 0.0
    slope = sum((xv - x_mean) * (yv - y_mean) for xv, yv in zip(x, y_slice)) / denom
    return float(slope * 60.0 / (2.0 * math.pi))


def window_rpms(
    angles: list[float], fps: int, window: int = 24, step: int = 6
) -> list[float]:
    if len(angles) < window + 1:
        return []
    values = unwrap_angles(angles)
    dt = float(window - 1) / float(max(1, fps))
    out: list[float] = []
    for start in range(0, len(values) - window, max(1, step)):
        delta = values[start + window - 1] - values[start]
        out.append(float(delta * 60.0 / (2.0 * math.pi * dt)))
    return out


def instant_rpms(angles: list[float], fps: int) -> list[float]:
    if len(angles) < 3:
        return []
    values = unwrap_angles(angles)
    deltas = np.diff(np.asarray(values, dtype=float))
    rpms = deltas * float(fps) * 60.0 / (2.0 * math.pi)
    return [float(value) for value in rpms]


def score_near(actual: float, target: float, tol: float) -> float:
    if tol <= 1e-9:
        return 1.0 if abs(actual - target) <= 1e-9 else 0.0
    return max(0.0, 1.0 - abs(actual - target) / tol)
