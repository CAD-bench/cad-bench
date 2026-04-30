from __future__ import annotations

import builtins
import contextvars
import importlib
import json
import math
import shutil
import signal
import subprocess
import tempfile

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from build123d import *  # noqa: F403
from bench.config import BUILD_SUCCESS_REWARD_WEIGHT, OVERALL_SCORE_REWARD_WEIGHT
from tasks.specs import (
    TaskModuleSpec,
    ensure_mcmaster_step_fixture,
    load_task_spec,
    task_meta,
    task_dir,
)

numpy = np

__all__ = ["ensure_mcmaster_step_fixture"]
LLM_GENERATED_CODE_TIMEOUT_SECONDS = 60 * 60
_LLM_GENERATED_CODE_WATCHDOG_ENABLED = contextvars.ContextVar(
    "llm_generated_code_watchdog_enabled", default=False
)
_SCORING_PROVENANCE_DIR = contextvars.ContextVar(
    "scoring_provenance_dir", default=None
)
_TASKS_PYTHON_DIR = Path(__file__).resolve().parent


class _BoundsIndexFallback:
    def __init__(self, bounds: Any):
        values = np.asanyarray(bounds, dtype=np.float64)
        if len(values.shape) == 3:
            if values.shape[1] != 2:
                raise ValueError("bounds not (n, 2, dimension)!")
            values = values.reshape((len(values), -1))
        elif len(values.shape) != 2 or values.size == 0:
            raise ValueError("Bounds must be (n, dimension * 2)!")
        if (values.shape[1] % 2) != 0:
            raise ValueError("Bounds must be (n,dimension*2)!")
        half = values.shape[1] // 2
        self._mins = values[:, :half]
        self._maxs = values[:, half:]
        self.bounds = np.concatenate(
            (self._mins.min(axis=0), self._maxs.max(axis=0))
        )

    def intersection(self, bounds: Any):
        query = np.asanyarray(bounds, dtype=np.float64).reshape(-1)
        half = query.shape[0] // 2
        query_min = query[:half]
        query_max = query[half:]
        hits = np.logical_and(self._maxs >= query_min, self._mins <= query_max).all(
            axis=1
        )
        return np.nonzero(hits)[0].tolist()


def _install_trimesh_bounds_tree_fallback() -> None:
    try:
        import rtree  # noqa: F401
    except BaseException:
        trimesh.util.bounds_tree = _BoundsIndexFallback


_install_trimesh_bounds_tree_fallback()


class _LLMGeneratedCodeWatchdog:
    def __init__(self, seconds: float):
        if seconds <= 0:
            raise ValueError("LLM-generated code timeout must be positive")
        self.seconds = float(seconds)
        self._previous_handler: Any = None

    def _handler(self, signum: int, frame: Any) -> None:
        del signum, frame
        raise TimeoutError(
            f"LLM-generated code exceeded {self.seconds:g}s execution limit"
        )

    def __enter__(self) -> None:
        self._previous_handler = signal.signal(signal.SIGALRM, self._handler)
        signal.setitimer(signal.ITIMER_REAL, self.seconds)

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        del exc_type, exc_val, exc_tb
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        if self._previous_handler is not None:
            signal.signal(signal.SIGALRM, self._previous_handler)


class _LLMGeneratedCodeContext:
    def __init__(self) -> None:
        self._token: contextvars.Token[bool] | None = None

    def __enter__(self) -> None:
        self._token = _LLM_GENERATED_CODE_WATCHDOG_ENABLED.set(True)

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        del exc_type, exc_val, exc_tb
        if self._token is not None:
            _LLM_GENERATED_CODE_WATCHDOG_ENABLED.reset(self._token)


def llm_generated_code_context() -> _LLMGeneratedCodeContext:
    return _LLMGeneratedCodeContext()


class _ScoringProvenanceContext:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._token: contextvars.Token[Path | None] | None = None

    def __enter__(self) -> Path:
        self.path.mkdir(parents=True, exist_ok=True)
        self._token = _SCORING_PROVENANCE_DIR.set(self.path)
        return self.path

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        del exc_type, exc_val, exc_tb
        if self._token is not None:
            _SCORING_PROVENANCE_DIR.reset(self._token)


def scoring_provenance_dir(path: str | Path) -> _ScoringProvenanceContext:
    return _ScoringProvenanceContext(Path(path))


def _blender_pythonpath_args() -> list[str]:
    return [
        "--python-expr",
        f"import sys; sys.path.insert(0, {str(_TASKS_PYTHON_DIR)!r})",
    ]


def _current_scoring_provenance_dir() -> Path | None:
    value = _SCORING_PROVENANCE_DIR.get()
    return Path(value) if isinstance(value, Path) else value


def _write_scoring_provenance_text(name: str, content: str) -> None:
    out_dir = _current_scoring_provenance_dir()
    if out_dir is None:
        return
    (out_dir / name).write_text(content, encoding="utf-8")


def _write_scoring_provenance_json(name: str, payload: Any) -> None:
    _write_scoring_provenance_text(name, json.dumps(payload, indent=2) + "\n")


def _copy_scoring_provenance_file(source: Path, name: str) -> None:
    out_dir = _current_scoring_provenance_dir()
    if out_dir is None or not source.exists():
        return
    shutil.copy2(source, out_dir / name)


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def near_score(value: float, target: float, tolerance: float) -> float:
    if tolerance <= 0:
        return 1.0 if value == target else 0.0
    error = abs(value - target)
    if error >= tolerance:
        return 0.0
    exact_band = 0.30 * tolerance
    if error <= exact_band:
        return 1.0
    return clamp01(1.0 - (error - exact_band) / max(tolerance - exact_band, 1e-9))


def _safe_import(
    name: str,
    globals_: Any = None,
    locals_: Any = None,
    fromlist: Any = (),
    level: int = 0,
) -> Any:
    del globals_, locals_, fromlist, level
    allowed = {
        "build123d",
        "math",
        "numpy",
    }
    root = name.split(".", 1)[0]
    if name not in allowed and root not in allowed:
        raise ImportError(f"Import of '{name}' is not allowed in this benchmark")
    return importlib.import_module(name)


def _safe_builtins() -> dict[str, Any]:
    names = [
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "enumerate",
        "float",
        "int",
        "len",
        "list",
        "max",
        "min",
        "pow",
        "print",
        "range",
        "round",
        "set",
        "str",
        "sum",
        "tuple",
        "zip",
    ]
    out = {name: getattr(builtins, name) for name in names}
    out["__import__"] = _safe_import
    return out


def _build123d_namespace() -> dict[str, Any]:
    import build123d as b3d

    ns: dict[str, Any] = {
        "__builtins__": _safe_builtins(),
        "np": np,
        "numpy": np,
        "math": math,
        "build123d": b3d,
        "b3d": b3d,
    }
    for name in dir(b3d):
        if not name.startswith("_"):
            ns[name] = getattr(b3d, name)
    return ns


def _unwrap_part_candidate(obj: Any) -> Any:
    if hasattr(obj, "part"):
        obj = obj.part
    if hasattr(obj, "tessellate") and hasattr(obj, "bounding_box"):
        return obj
    return None


def _extract_part(
    namespace: dict[str, Any], *, initial_keys: set[str] | None = None
) -> Any:
    for key in ("part", "result", "model", "screw"):
        if key in namespace:
            obj = _unwrap_part_candidate(namespace[key])
            if obj is not None:
                return obj

    for fn_name in ("build_part", "build", "make_part", "make_model"):
        fn = namespace.get(fn_name)
        if callable(fn):
            obj = _unwrap_part_candidate(fn())
            if obj is not None:
                return obj

    if initial_keys is not None:
        candidates: list[Any] = []
        for key, value in namespace.items():
            if key in initial_keys or key.startswith("__"):
                continue
            obj = _unwrap_part_candidate(value)
            if obj is not None:
                candidates.append(obj)
        if len(candidates) == 1:
            return candidates[0]

    raise ValueError(
        "Could not find a Build123D result. Define `part`, a zero-arg builder "
        "function, or a single unambiguous Build123D object variable."
    )


def execute_candidate_code(code: str) -> Any:
    namespace = _build123d_namespace()
    initial_keys = set(namespace)
    compiled = compile(code, "<candidate>", "exec")
    exec(compiled, namespace, namespace)
    return _extract_part(namespace, initial_keys=initial_keys)


def execute_llm_generated_code(
    code: str, timeout_seconds: float = LLM_GENERATED_CODE_TIMEOUT_SECONDS
) -> Any:
    with _LLMGeneratedCodeWatchdog(timeout_seconds):
        return execute_candidate_code(code)


def execute_submission_code(code: str) -> Any:
    if _LLM_GENERATED_CODE_WATCHDOG_ENABLED.get():
        return execute_llm_generated_code(code)
    return execute_candidate_code(code)


def mesh_from_part(part: Any, tolerance: float = 0.03) -> trimesh.Trimesh:
    verts, faces = part.tessellate(tolerance)
    if not verts or not faces:
        raise ValueError("Tessellation produced no mesh")
    vertices = np.array([[v.X, v.Y, v.Z] for v in verts], dtype=float)
    mesh = trimesh.Trimesh(vertices=vertices, faces=np.array(faces), process=True)
    if hasattr(mesh, "nondegenerate_faces"):
        mesh.update_faces(mesh.nondegenerate_faces())
    if hasattr(mesh, "unique_faces"):
        mesh.update_faces(mesh.unique_faces())
    mesh.remove_unreferenced_vertices()
    if hasattr(mesh, "merge_vertices"):
        mesh.merge_vertices(digits_vertex=6)
    if len(mesh.vertices) < 8 or len(mesh.faces) < 8:
        raise ValueError("Mesh is too small to score")
    return mesh


def _task_asset_path(task_id: str, filename: str) -> Path:
    path = (task_dir(task_id) / filename).resolve()
    if not path.exists():
        raise FileNotFoundError(f"missing task asset for {task_id}: {path}")
    return path


def _task_spec_for_id(task_id: str) -> TaskModuleSpec:
    return load_task_spec(task_id)


_clamp01 = clamp01
_near_score = near_score
_execute_candidate_code = execute_candidate_code
_mesh_from_part = mesh_from_part
REFERENCE_GEOMETRY_GATE_TOLERANCE_FRAC = 0.12


def _slug(name: str) -> str:
    return name.replace("/", "_").replace(" ", "_")


def _z_hits(mesh: trimesh.Trimesh, x: float, y: float) -> list[float]:
    zmax = float(np.max(mesh.vertices[:, 2]))
    origins = np.array([[x, y, zmax + 2.0]], dtype=float)
    dirs = np.array([[0.0, 0.0, -1.0]], dtype=float)
    hits, _, _ = mesh.ray.intersects_location(origins, dirs, multiple_hits=True)
    if len(hits) == 0:
        return []
    vals = sorted((float(v) for v in hits[:, 2]), reverse=True)
    unique_vals: list[float] = []
    for val in vals:
        if not unique_vals or abs(val - unique_vals[-1]) > 1e-4:
            unique_vals.append(val)
    return unique_vals


def _bbox_metrics(mesh: trimesh.Trimesh) -> dict[str, float]:
    mins = np.min(mesh.vertices, axis=0)
    maxs = np.max(mesh.vertices, axis=0)
    size = maxs - mins
    return {
        "min_x": float(mins[0]),
        "min_y": float(mins[1]),
        "min_z": float(mins[2]),
        "max_x": float(maxs[0]),
        "max_y": float(maxs[1]),
        "max_z": float(maxs[2]),
        "size_x": float(size[0]),
        "size_y": float(size[1]),
        "size_z": float(size[2]),
        "center_x": float((mins[0] + maxs[0]) / 2.0),
        "center_y": float((mins[1] + maxs[1]) / 2.0),
        "center_z": float((mins[2] + maxs[2]) / 2.0),
    }


def _estimate_outer_d(mesh: trimesh.Trimesh) -> float:
    r = np.linalg.norm(mesh.vertices[:, :2], axis=1)
    return float(2.0 * np.quantile(r, 0.998))


def _estimate_bore_d(mesh: trimesh.Trimesh, max_r: float = 4.0) -> float:
    radii = np.linspace(0.0, max_r, 201)
    open_r = 0.0
    for radius in radii:
        hits = _z_hits(mesh, float(radius), 0.0)
        if len(hits) == 0:
            open_r = float(radius)
    return 2.0 * open_r


def _radial_hit_radius(mesh: trimesh.Trimesh, angle_rad: float, z: float) -> float:
    c = float(np.cos(angle_rad))
    s = float(np.sin(angle_rad))
    origin = np.array([[0.0, 0.0, z]], dtype=float)
    direction = np.array([[c, s, 0.0]], dtype=float)
    hits, _, _ = mesh.ray.intersects_location(origin, direction, multiple_hits=True)
    if len(hits) == 0:
        return 0.0
    vectors = hits - origin[0]
    proj = vectors @ direction[0]
    proj = proj[proj > 1e-6]
    if len(proj) == 0:
        return 0.0
    return float(np.max(proj))


def _estimate_tooth_count(mesh: trimesh.Trimesh, z: float) -> tuple[float, float]:
    n = 256
    angles = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
    radii = np.array(
        [_radial_hit_radius(mesh, float(a), z) for a in angles], dtype=float
    )
    radii_centered = radii - np.mean(radii)
    fft = np.fft.rfft(radii_centered)
    mags = np.abs(fft)
    if len(mags) < 4:
        return 0.0, 0.0
    mags[0] = 0.0
    max_k = min(64, len(mags) - 1)
    k = int(np.argmax(mags[1 : max_k + 1]) + 1)
    amp = float(np.std(radii))
    return float(k), amp


def _gear_profile_metrics(
    mesh: trimesh.Trimesh, z: float, n_angles: int = 720
) -> dict[str, float]:
    """Estimate coarse tooth-shape fidelity from a radial sweep at mid-plane."""
    angles = np.linspace(0.0, 2.0 * np.pi, n_angles, endpoint=False)
    radii = np.array(
        [_radial_hit_radius(mesh, float(a), z) for a in angles], dtype=float
    )

    r30 = float(np.quantile(radii, 0.30))
    r70 = float(np.quantile(radii, 0.70))
    tooth_depth = max(0.0, r70 - r30)

    centered = radii - float(np.mean(radii))
    mags = np.abs(np.fft.rfft(centered))
    if len(mags) < 4:
        spectral_purity = 0.0
    else:
        mags[0] = 0.0
        max_k = min(64, len(mags) - 1)
        band = mags[1 : max_k + 1]
        dominant = float(np.max(band))
        spectral_purity = dominant / (float(np.sum(band)) + 1e-9)

    return {
        "root_radius_q30": r30,
        "tooth_band_radius_q70": r70,
        "tooth_depth_q70_q30": float(tooth_depth),
        "spectral_purity": float(spectral_purity),
    }


def _conservative_composite(
    pose: float,
    dims: float,
    task: float,
    *,
    pose_weight: float,
    dims_weight: float,
    task_weight: float,
) -> float:
    base = (
        pose_weight * _clamp01(pose)
        + dims_weight * _clamp01(dims)
        + task_weight * _clamp01(task)
    )
    pose_gate = 0.08 + 0.92 * (_clamp01(pose) ** 3)
    task_gate = 0.25 + 0.75 * _clamp01(task)
    return _clamp01(base * pose_gate * task_gate)


def normalize_score_result(raw: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(raw)
    metrics["build_success"] = float(metrics.get("build_success", 0.0))
    metrics["task_score"] = float(
        metrics.get("task_score", metrics.get("task_specific_score", 0.0))
    )
    metrics["geometry_score"] = float(metrics.get("geometry_score", 0.0))
    metrics["task_specific_score"] = float(
        metrics.get("task_specific_score", metrics["task_score"])
    )
    metrics["overall_score"] = float(
        metrics.get(
            "overall_score",
            metrics.get("composite", metrics.get("reward", metrics["task_score"])),
        )
    )
    metrics["reward"] = (
        BUILD_SUCCESS_REWARD_WEIGHT * metrics["build_success"]
        + OVERALL_SCORE_REWARD_WEIGHT * metrics["overall_score"]
    )
    metrics.pop("composite", None)
    return metrics


def _component_radial_profile(
    points: np.ndarray, cx: float, cy: float
) -> dict[str, float]:
    if len(points) == 0:
        return {"r_min": 0.0, "r_eff": 0.0, "r_max": 0.0}
    r = np.linalg.norm(points[:, :2] - np.array([cx, cy], dtype=float), axis=1)
    return {
        "r_min": float(np.quantile(r, 0.02)),
        "r_eff": float(np.quantile(r, 0.86)),
        "r_max": float(np.quantile(r, 0.98)),
    }


def _component_attached_to_axle(
    points: np.ndarray, cx: float, cy: float, axle_r: float, z_min: float, z_max: float
) -> bool:
    if len(points) == 0:
        return False
    z = points[:, 2]
    if float(np.max(z)) < z_min + 0.2 or float(np.min(z)) > z_max - 0.2:
        return False
    p = _component_radial_profile(points, cx, cy)
    near_axle = p["r_min"] <= axle_r + 0.95
    has_body = p["r_max"] >= axle_r + 1.8
    return bool(near_axle and has_body)


def _axle_angular_coverage(
    points: np.ndarray, cx: float, cy: float, r_eff: float, axle_r: float
) -> float:
    if len(points) < 64:
        return 0.0
    radial = np.linalg.norm(points[:, :2] - np.array([cx, cy], dtype=float), axis=1)
    band_lo = max(axle_r + 0.5, r_eff - 1.2)
    band_hi = r_eff + 1.2
    band = (radial >= band_lo) & (radial <= band_hi)
    if int(np.sum(band)) < 48:
        return 0.0
    theta = np.arctan2(points[band, 1] - cy, points[band, 0] - cx)
    n_bins = 72
    bins = np.floor(((theta + np.pi) / (2.0 * np.pi)) * n_bins).astype(int)
    bins = np.clip(bins, 0, n_bins - 1)
    return float(len(set(bins.tolist())) / n_bins)


def _inside_d_shaft_xy(
    dx: float, dy: float, axle_r: float, flat_x_from_center: float
) -> bool:
    if dx * dx + dy * dy > axle_r * axle_r:
        return False
    return dx <= flat_x_from_center


def _d_shaft_profile_radius(
    angle_rad: float, axle_r: float, flat_x_from_center: float
) -> float:
    radius = float(axle_r)
    cos_a = float(np.cos(angle_rad))
    if cos_a > 1e-6:
        radius = min(radius, float(flat_x_from_center) / cos_a)
    return max(0.0, radius)


def _mesh_collides_axle(
    mesh: trimesh.Trimesh,
    cx: float,
    cy: float,
    axle_r: float,
    z_min: float,
    z_max: float,
    flat_x_from_center: float | None = None,
) -> bool:
    flat_x = float(flat_x_from_center if flat_x_from_center is not None else axle_r)
    probes = [
        (dx, dy)
        for dx, dy in (
            (0.0, 0.0),
            (0.60 * axle_r, 0.0),
            (0.20 * axle_r, 0.55 * axle_r),
            (0.20 * axle_r, -0.55 * axle_r),
            (-0.45 * axle_r, 0.0),
            (-0.25 * axle_r, 0.55 * axle_r),
            (-0.25 * axle_r, -0.55 * axle_r),
        )
        if _inside_d_shaft_xy(dx, dy, axle_r, flat_x)
    ]
    for dx, dy in probes:
        hits = _z_hits(mesh, cx + dx, cy + dy)
        if len(hits) < 2:
            continue
        top = float(max(hits))
        bot = float(min(hits))
        overlap = min(top, z_max + 0.1) - max(bot, z_min - 0.1)
        if overlap > 0.15:
            return True
    return False


def _downsample_mesh_faces(
    mesh: trimesh.Trimesh, max_faces: int = 8000
) -> trimesh.Trimesh:
    if len(mesh.faces) <= max_faces:
        return mesh
    simplified = mesh.simplify_quadric_decimation(face_count=int(max_faces))
    if not isinstance(simplified, trimesh.Trimesh):
        raise RuntimeError("quadric_decimation_failed")
    if len(simplified.faces) < 128:
        raise RuntimeError("quadric_decimation_too_small")
    simplified.remove_unreferenced_vertices()
    return simplified


def _repair_mesh_for_sim(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    m = mesh.copy()
    m.remove_unreferenced_vertices()
    trimesh.repair.fill_holes(m)
    m.remove_unreferenced_vertices()
    return m


def _localize_mesh(mesh: trimesh.Trimesh, cx: float, cy: float) -> trimesh.Trimesh:
    local = mesh.copy()
    local.vertices = np.asarray(local.vertices, dtype=float) - np.array(
        [cx, cy, 0.0], dtype=float
    )
    return local


def _solid_bbox_metrics(part: Any) -> dict[str, float]:
    bb = part.bounding_box()
    return {
        "min_x": float(bb.min.X),
        "min_y": float(bb.min.Y),
        "min_z": float(bb.min.Z),
        "max_x": float(bb.max.X),
        "max_y": float(bb.max.Y),
        "max_z": float(bb.max.Z),
        "size_x": float(bb.size.X),
        "size_y": float(bb.size.Y),
        "size_z": float(bb.size.Z),
        "center_x": float((bb.min.X + bb.max.X) * 0.5),
        "center_y": float((bb.min.Y + bb.max.Y) * 0.5),
        "center_z": float((bb.min.Z + bb.max.Z) * 0.5),
    }


def _shape_volume(shape: Any) -> float:
    if shape is None:
        return 0.0
    volume = getattr(shape, "volume", None)
    if volume is not None:
        return max(0.0, float(volume))
    if isinstance(shape, (list, tuple)):
        return float(sum(_shape_volume(item) for item in shape))
    if hasattr(shape, "__iter__"):
        return float(sum(_shape_volume(item) for item in shape))
    return 0.0


def _reference_part_for_task(task_id: str) -> Any:
    spec = _task_spec_for_id(task_id)
    return _execute_candidate_code(spec.reference_solution_code)


def _reference_geometry_gate(task_id: str, part: Any) -> tuple[float, float]:
    reference = _reference_part_for_task(task_id)
    reference_volume = _shape_volume(reference)
    extra_volume = _shape_volume(part.cut(reference))
    missing_volume = _shape_volume(reference.cut(part))
    diff_fraction = (extra_volume + missing_volume) / max(reference_volume, 1e-9)
    base = _clamp01(1.0 - diff_fraction / REFERENCE_GEOMETRY_GATE_TOLERANCE_FRAC)
    gate = base * base
    return float(gate), float(diff_fraction)


def _apply_reference_geometry_gate(
    task_id: str, part: Any, raw: dict[str, Any]
) -> dict[str, Any]:
    gated = dict(raw)
    gate, diff_fraction = _reference_geometry_gate(task_id, part)
    base_overall = float(
        gated.get(
            "overall_score",
            gated.get("composite", gated.get("reward", gated.get("task_score", 0.0))),
        )
    )
    base_geometry = float(gated.get("geometry_score", 0.0))
    gated["overall_score"] = float(base_overall * gate)
    gated["geometry_score"] = float(base_geometry * gate)
    gated["reference_geometry_gate"] = float(gate)
    gated["reference_geometry_diff_fraction"] = float(diff_fraction)
    return gated


def _radial_first_hit_radius(
    mesh: trimesh.Trimesh, angle_rad: float, z: float
) -> float:
    c = float(np.cos(angle_rad))
    s = float(np.sin(angle_rad))
    origin = np.array([[0.0, 0.0, z]], dtype=float)
    direction = np.array([[c, s, 0.0]], dtype=float)
    hits, _, _ = mesh.ray.intersects_location(origin, direction, multiple_hits=True)
    if len(hits) == 0:
        return 0.0
    vectors = hits - origin[0]
    proj = vectors @ direction[0]
    proj = proj[proj > 1e-6]
    if len(proj) == 0:
        return 0.0
    return float(np.min(proj))


def _estimate_pitch_from_signal(
    values: np.ndarray, dz: float, expected_pitch: float
) -> float:
    if len(values) < 16 or dz <= 1e-9:
        return 0.0
    centered = values - float(np.mean(values))
    mags = np.abs(np.fft.rfft(centered))
    freqs = np.fft.rfftfreq(len(centered), d=dz)
    if len(mags) < 3:
        return 0.0
    mags[0] = 0.0
    lo = max(1, int(np.searchsorted(freqs, 0.4 / max(0.2, expected_pitch))))
    hi = min(
        len(freqs) - 1, int(np.searchsorted(freqs, 1.8 / max(0.2, expected_pitch)))
    )
    if hi <= lo:
        return 0.0
    k = int(np.argmax(mags[lo : hi + 1]) + lo)
    if freqs[k] <= 1e-9:
        return 0.0
    freq = float(freqs[k])
    if 1 <= k < len(mags) - 1:
        y0 = float(np.log(mags[k - 1] + 1e-12))
        y1 = float(np.log(mags[k] + 1e-12))
        y2 = float(np.log(mags[k + 1] + 1e-12))
        denom = y0 - 2.0 * y1 + y2
        if abs(denom) > 1e-12:
            delta = 0.5 * (y0 - y2) / denom
            delta = float(np.clip(delta, -1.0, 1.0))
            if len(freqs) >= 2:
                freq += delta * float(freqs[1] - freqs[0])
    if freq <= 1e-9:
        return 0.0
    return float(1.0 / freq)


def _thread_signal_metrics(
    mesh: trimesh.Trimesh,
    z_min: float,
    z_max: float,
    expected_pitch: float,
    inner: bool,
) -> dict[str, float]:
    angles = [0.0, np.pi / 3.0, 2.0 * np.pi / 3.0]
    zs = np.linspace(z_min, z_max, 72)
    best_amp = -1.0
    best_pitch = 0.0
    best_hi = 0.0
    best_lo = 0.0
    for angle in angles:
        vals = np.array(
            [
                (
                    _radial_first_hit_radius(mesh, float(angle), float(z))
                    if inner
                    else _radial_hit_radius(mesh, float(angle), float(z))
                )
                for z in zs
            ],
            dtype=float,
        )
        vals = vals[vals > 0.0]
        if len(vals) < 24:
            continue
        lo = float(np.quantile(vals, 0.10))
        hi = float(np.quantile(vals, 0.90))
        amp = hi - lo
        pitch_est = _estimate_pitch_from_signal(
            vals, float(zs[1] - zs[0]), expected_pitch
        )
        if amp > best_amp:
            best_amp = amp
            best_pitch = pitch_est
            best_hi = hi
            best_lo = lo
    return {
        "amp_r": max(0.0, float(best_amp)),
        "pitch_est": float(best_pitch),
        "r_hi": float(best_hi),
        "r_lo": float(best_lo),
    }


def _thread_profile_signature(
    mesh: trimesh.Trimesh,
    z_values: np.ndarray,
    *,
    inner: bool,
    n_angles: int = 48,
) -> np.ndarray:
    sampler = _radial_first_hit_radius if inner else _radial_hit_radius
    angles = np.linspace(0.0, 2.0 * np.pi, n_angles, endpoint=False)
    rows = [
        [sampler(mesh, float(angle), float(z)) for angle in angles] for z in z_values
    ]
    return np.asarray(rows, dtype=float)


def _circular_profile_match_score(
    candidate: np.ndarray, reference: np.ndarray, tolerance: float
) -> float:
    if candidate.shape != reference.shape or candidate.size == 0:
        return 0.0
    best_error = float("inf")
    for shift in range(reference.shape[1]):
        err = float(np.mean(np.abs(np.roll(candidate, shift, axis=1) - reference)))
        if err < best_error:
            best_error = err
    return _near_score(best_error, 0.0, tolerance)


def _custom_thread_reference_profiles() -> dict[str, Any]:
    task_id = "custom_threaded_pair_nonstandard"
    spec = _task_spec_for_id(task_id)
    part = _execute_candidate_code(spec.reference_solution_code)
    bolt_solid, nut_solid = _select_thread_pair_solids(part, spec.expected)
    if bolt_solid is None or nut_solid is None:
        raise ValueError("custom thread reference pair is missing solids")

    bolt_mesh = _localize_mesh(
        _mesh_from_part(bolt_solid),
        float(spec.expected["bolt_center_x_mm"]),
        float(spec.expected["bolt_center_y_mm"]),
    )
    nut_mesh = _localize_mesh(
        _mesh_from_part(nut_solid),
        float(spec.expected["nut_center_x_mm"]),
        float(spec.expected["nut_center_y_mm"]),
    )

    bolt_z = np.linspace(0.8, float(spec.expected["thread_length_mm"]) - 0.4, 7)
    nut_z = np.linspace(
        0.8,
        min(
            float(spec.expected["nut_h_mm"]) - 0.6,
            float(spec.expected["thread_length_mm"]) - 0.4,
        ),
        7,
    )
    return {
        "bolt_z": bolt_z,
        "nut_z": nut_z,
        "bolt_profile": _thread_profile_signature(bolt_mesh, bolt_z, inner=False),
        "nut_profile": _thread_profile_signature(nut_mesh, nut_z, inner=True),
    }


M3_TASK_ID = "m3x6_socket_head_zminus"
M3_REFERENCE_STEP_PART_NUMBER = "91290A111"


def _m3_vertical_hit_values(mesh: trimesh.Trimesh, x: float, y: float) -> list[float]:
    origins = np.array([[x, y, 2.0]], dtype=float)
    dirs = np.array([[0.0, 0.0, -1.0]], dtype=float)
    hits, _, _ = mesh.ray.intersects_location(origins, dirs, multiple_hits=True)
    if len(hits) == 0:
        return []
    vals = sorted((float(v) for v in hits[:, 2]), reverse=True)
    unique_vals: list[float] = []
    for val in vals:
        if not unique_vals or abs(val - unique_vals[-1]) > 1e-4:
            unique_vals.append(val)
    return unique_vals


def _m3_first_vertical_hit_z(mesh: trimesh.Trimesh, x: float, y: float) -> float | None:
    hits = _m3_vertical_hit_values(mesh, x, y)
    return hits[0] if hits else None


def _m3_pca_axis_alignment(surface_points: np.ndarray) -> float:
    centered = surface_points - np.mean(surface_points, axis=0, keepdims=True)
    cov = np.cov(centered, rowvar=False)
    _, eigvecs = np.linalg.eigh(cov)
    axis = eigvecs[:, -1]
    return float(abs(axis[2]))


def _m3_socket_open_profile(
    mesh: trimesh.Trimesh, radius: float, n_angles: int = 72
) -> tuple[float, float]:
    opened = []
    for i in range(n_angles):
        angle = 2.0 * math.pi * i / n_angles
        x = radius * math.cos(angle)
        y = radius * math.sin(angle)
        hit = _m3_first_vertical_hit_z(mesh, x, y)
        opened.append(1.0 if hit is not None and hit < -0.2 else 0.0)
    arr = np.array(opened, dtype=float)
    fft = np.fft.rfft(arr - np.mean(arr))
    denom = float(np.sum(np.abs(fft)) + 1e-9)
    k6 = float(abs(fft[6]) / denom) if len(fft) > 6 else 0.0
    return float(np.mean(arr)), k6


def _m3_estimate_socket_af(mesh: trimesh.Trimesh, target_af: float) -> float:
    target_radius = target_af / 2.0
    radii = np.linspace(target_radius * 0.8, target_radius * 1.2, 13)
    fracs = []
    for radius in radii:
        frac, _ = _m3_socket_open_profile(mesh, float(radius))
        fracs.append(frac)
    for i in range(len(radii) - 1):
        a0, a1 = fracs[i], fracs[i + 1]
        if (a0 >= 0.5 and a1 <= 0.5) or (a0 <= 0.5 and a1 >= 0.5):
            t = (0.5 - a0) / (a1 - a0 + 1e-9)
            r50 = float(radii[i] + t * (radii[i + 1] - radii[i]))
            return 2.0 * r50
    idx = int(np.argmin(np.abs(np.array(fracs) - 0.5)))
    return 2.0 * float(radii[idx])


def _m3_helix_fit_confidence(
    points: np.ndarray, expected_pitch: float
) -> tuple[float, float, str]:
    z = points[:, 2]
    theta = np.arctan2(points[:, 1], points[:, 0])
    pitches = np.linspace(0.35, 0.8, 181)
    best_conf = 0.0
    best_pitch = expected_pitch
    best_hand = "right"
    for hand, sign in (("right", -1.0), ("left", 1.0)):
        for pitch in pitches:
            phase = theta + sign * 2.0 * math.pi * z / pitch
            conf = float(abs(np.mean(np.exp(1j * phase))))
            if conf > best_conf:
                best_conf = conf
                best_pitch = float(pitch)
                best_hand = hand
    return best_conf, best_pitch, best_hand


def _m3_collect_observables(
    part: Any, mesh: trimesh.Trimesh, expected: dict[str, Any]
) -> dict[str, float]:
    np.random.seed(0)
    surface_points, _ = trimesh.sample.sample_surface(mesh, 120_000)
    bbox = part.bounding_box()
    min_z = float(bbox.min.Z)
    max_z = float(bbox.max.Z)
    z = surface_points[:, 2]
    r = np.linalg.norm(surface_points[:, :2], axis=1)
    axis_align = _m3_pca_axis_alignment(surface_points)
    head_h_expected = float(expected["head_h_mm"])
    underhead_expected = float(expected["underhead_len_mm"])
    head_zone = (z > -(head_h_expected - 0.05)) & (z < -0.05)
    thread_low = min_z + 0.2
    thread_high = min_z + underhead_expected - 0.2
    thread_zone = (z > thread_low) & (z < thread_high)
    head_d_est = (
        float(2.0 * np.quantile(r[head_zone], 0.995)) if np.any(head_zone) else 0.0
    )
    major_d_est = (
        float(2.0 * np.quantile(r[thread_zone], 0.50)) if np.any(thread_zone) else 0.0
    )
    sample_x = 0.25 * (float(expected["head_d_mm"]) + float(expected["major_d_mm"]))
    side_hits = _m3_vertical_hit_values(mesh, sample_x, 0.0)
    head_underside_z = min_z + underhead_expected
    head_h_est = 0.0
    if len(side_hits) >= 2:
        top = side_hits[0]
        underside = None
        for candidate in side_hits[1:]:
            if candidate < top - 0.1:
                underside = candidate
                break
        if underside is not None:
            head_underside_z = underside
            head_h_est = top - underside
    underhead_len_est = head_underside_z - min_z
    socket_depth_est = 0.0
    center_hit = _m3_first_vertical_hit_z(mesh, 0.0, 0.0)
    if center_hit is not None and center_hit < -0.05:
        socket_depth_est = -center_hit
    target_af = float(expected["socket_af_mm"])
    target_radius = target_af / 2.0
    open_r_inner, _ = _m3_socket_open_profile(mesh, 0.88 * target_radius)
    open_r_mid, k6_r_mid = _m3_socket_open_profile(mesh, 1.04 * target_radius)
    open_r_outer, _ = _m3_socket_open_profile(mesh, 1.08 * target_radius)
    socket_af_est = _m3_estimate_socket_af(mesh, target_af)
    helix_conf = 0.0
    pitch_est = float(expected["pitch_mm"])
    handedness = "right"
    if np.any(thread_zone):
        tz_points = surface_points[thread_zone]
        tz_r = np.linalg.norm(tz_points[:, :2], axis=1)
        cutoff = float(np.quantile(tz_r, 0.93))
        crest_points = tz_points[tz_r >= cutoff]
        if len(crest_points) >= 1000:
            helix_conf, pitch_est, handedness = _m3_helix_fit_confidence(
                crest_points,
                float(expected["pitch_mm"]),
            )
    turns_est = underhead_len_est / pitch_est if pitch_est > 1e-8 else 0.0
    return {
        "build_success": 1.0,
        "max_z": max_z,
        "min_z": min_z,
        "axis_alignment": axis_align,
        "head_d_est": head_d_est,
        "head_h_est": head_h_est,
        "underhead_len_est": underhead_len_est,
        "major_d_est": major_d_est,
        "socket_depth_est": socket_depth_est,
        "socket_af_est": socket_af_est,
        "open_r_inner": open_r_inner,
        "open_r_mid": open_r_mid,
        "open_r_outer": open_r_outer,
        "k6_r_mid": k6_r_mid,
        "helix_confidence": helix_conf,
        "pitch_est": pitch_est,
        "thread_is_right": 1.0 if handedness == "right" else 0.0,
        "turns_est": turns_est,
    }


@lru_cache(maxsize=1)
def _m3_reference_observables() -> dict[str, float]:
    ensure_mcmaster_step_fixture(
        M3_TASK_ID,
        str(task_meta(M3_TASK_ID)["reference_step_fixture"]),
        M3_REFERENCE_STEP_PART_NUMBER,
    )
    spec = load_task_spec(M3_TASK_ID)
    part = _execute_candidate_code(spec.reference_solution_code)
    mesh = _mesh_from_part(part)
    return _m3_collect_observables(part, mesh, spec.expected)


def _m3_weighted_mean(
    scores: dict[str, float], weights: dict[str, float], names: tuple[str, ...]
) -> float:
    total_weight = sum(weights.get(name, 0.0) for name in names)
    if total_weight <= 0.0:
        return 0.0
    return sum(scores[name] * weights.get(name, 0.0) for name in names) / total_weight


def _eval_hard_m3_socket_head_screw(
    part: Any, mesh: trimesh.Trimesh, expected: dict[str, Any]
) -> dict[str, Any]:
    expected_floats = {str(key): float(value) for key, value in dict(expected).items()}
    weights = {
        str(key): float(value)
        for key, value in dict(task_meta(M3_TASK_ID).get("module_weights", {})).items()
    }
    obs = _m3_collect_observables(part, mesh, expected_floats)
    gold = _m3_reference_observables()

    pose_scores = {
        "half_space_score": (
            1.0
            if obs["max_z"] <= 0.03
            else clamp01(1.0 - (obs["max_z"] - 0.03) / 0.3)
        ),
        "top_plane_score": near_score(obs["max_z"], 0.0, 0.05),
        "min_z_score": near_score(
            obs["min_z"],
            -(expected_floats["head_h_mm"] + expected_floats["underhead_len_mm"]),
            0.5,
        ),
        "axis_score": clamp01((obs["axis_alignment"] - 0.92) / 0.08),
    }
    module_generic_pose = (
        0.28 * pose_scores["half_space_score"]
        + 0.24 * pose_scores["top_plane_score"]
        + 0.24 * pose_scores["min_z_score"]
        + 0.24 * pose_scores["axis_score"]
    )

    dimension_scores = {
        "head_d_score": near_score(obs["head_d_est"], gold["head_d_est"], 0.30),
        "head_h_score": near_score(obs["head_h_est"], gold["head_h_est"], 0.25),
        "underhead_score": near_score(
            obs["underhead_len_est"], gold["underhead_len_est"], 0.30
        ),
        "major_d_score": near_score(obs["major_d_est"], gold["major_d_est"], 0.20),
    }
    module_generic_dimensions = (
        0.30 * dimension_scores["head_d_score"]
        + 0.25 * dimension_scores["head_h_score"]
        + 0.25 * dimension_scores["underhead_score"]
        + 0.20 * dimension_scores["major_d_score"]
    )

    contact_scores = {
        "socket_depth_score": near_score(
            obs["socket_depth_est"], gold["socket_depth_est"], 0.20
        ),
        "socket_af_score": near_score(obs["socket_af_est"], gold["socket_af_est"], 0.20),
        "open_inner_score": near_score(obs["open_r_inner"], gold["open_r_inner"], 0.10),
        "open_outer_score": near_score(obs["open_r_outer"], gold["open_r_outer"], 0.10),
        "harmonic_score": near_score(obs["k6_r_mid"], gold["k6_r_mid"], 0.10),
    }
    module_generic_contact = (
        0.25 * contact_scores["socket_depth_score"]
        + 0.15 * contact_scores["socket_af_score"]
        + 0.20 * contact_scores["open_inner_score"]
        + 0.15 * contact_scores["open_outer_score"]
        + 0.25 * contact_scores["harmonic_score"]
    )

    thread_scores = {
        "thread_presence": clamp01((obs["helix_confidence"] - 0.2) / 0.6),
        "pitch_score": (
            near_score(obs["pitch_est"], expected_floats["pitch_mm"], 0.08)
            if obs["helix_confidence"] > 0.2
            else 0.0
        ),
        "handed_score": clamp01(obs["thread_is_right"]),
    }
    module_task_thread = (
        0.45 * thread_scores["thread_presence"]
        + 0.40 * thread_scores["pitch_score"]
        + 0.15 * thread_scores["handed_score"]
    )

    insertion_scores = {
        "turns_score": near_score(obs["turns_est"], gold["turns_est"], 0.6),
        "diameter_fit": near_score(obs["major_d_est"], gold["major_d_est"], 0.20),
    }
    insertion_scores["drive_margin"] = clamp01(
        (module_generic_contact + insertion_scores["diameter_fit"]) / 2.0
    )
    insertion_scores["insertion_success"] = (
        module_task_thread * insertion_scores["drive_margin"]
    )
    module_task_insertion = (
        insertion_scores["insertion_success"] * insertion_scores["turns_score"]
    )

    module_scores = {
        "generic_pose": clamp01(module_generic_pose),
        "generic_dimensions": clamp01(module_generic_dimensions),
        "generic_contact": clamp01(module_generic_contact),
        "task_thread": clamp01(module_task_thread),
        "task_insertion": clamp01(module_task_insertion),
    }
    generic_names = ("generic_pose", "generic_dimensions", "generic_contact")
    task_names = ("task_thread", "task_insertion")
    overall_score = clamp01(
        _m3_weighted_mean(module_scores, weights, tuple(module_scores.keys()))
    )
    generic_score = clamp01(_m3_weighted_mean(module_scores, weights, generic_names))
    task_score = clamp01(_m3_weighted_mean(module_scores, weights, task_names))
    out = {
        "build_success": 1.0,
        "overall_score": overall_score,
        "reward": overall_score,
        "layer_generic_score": generic_score,
        "layer_task_score": task_score,
        "task_score": task_score,
        "thread_score": module_scores["task_thread"],
        "socket_score": module_scores["generic_contact"],
        "simulation_score": module_scores["task_insertion"],
        "pitch_est": obs["pitch_est"],
        "turns_est": obs["turns_est"],
        "axis_alignment": obs["axis_alignment"],
        "head_d_est": obs["head_d_est"],
        "head_h_est": obs["head_h_est"],
        "major_d_est": obs["major_d_est"],
        "underhead_len_est": obs["underhead_len_est"],
        "socket_depth_est": obs["socket_depth_est"],
        "socket_af_est": obs["socket_af_est"],
        "helix_confidence": obs["helix_confidence"],
        "open_r_inner": obs["open_r_inner"],
        "open_r_mid": obs["open_r_mid"],
        "open_r_outer": obs["open_r_outer"],
        "k6_r_mid": obs["k6_r_mid"],
        "max_z": obs["max_z"],
        "min_z": obs["min_z"],
        "module_generic_pose": module_scores["generic_pose"],
        "module_generic_dimensions": module_scores["generic_dimensions"],
        "module_generic_contact": module_scores["generic_contact"],
        "module_task_thread": module_scores["task_thread"],
        "module_task_insertion": module_scores["task_insertion"],
    }
    for key, value in pose_scores.items():
        out[f"generic_pose_{key}"] = float(value)
    for key, value in dimension_scores.items():
        out[f"generic_dimensions_{key}"] = float(value)
    for key, value in contact_scores.items():
        out[f"generic_contact_{key}"] = float(value)
    for key, value in thread_scores.items():
        out[f"task_thread_{key}"] = float(value)
    for key, value in insertion_scores.items():
        out[f"task_insertion_{key}"] = float(value)
    return out


def _select_thread_pair_solids(
    part: Any, expected: dict[str, float]
) -> tuple[Any | None, Any | None]:
    solids = list(part.solids()) if hasattr(part, "solids") else []
    if not solids and part is not None:
        solids = [part]
    if not solids:
        return None, None
    bolt_target = np.array(
        [float(expected["bolt_center_x_mm"]), float(expected["bolt_center_y_mm"])],
        dtype=float,
    )
    nut_target = np.array(
        [float(expected["nut_center_x_mm"]), float(expected["nut_center_y_mm"])],
        dtype=float,
    )
    scored: list[tuple[float, float, Any]] = []
    for solid in solids:
        bb = _solid_bbox_metrics(solid)
        ctr = np.array([bb["center_x"], bb["center_y"]], dtype=float)
        scored.append(
            (
                float(np.linalg.norm(ctr - bolt_target)),
                float(np.linalg.norm(ctr - nut_target)),
                solid,
            )
        )
    bolt = min(scored, key=lambda item: item[0])[2] if scored else None
    remaining = [item for item in scored if item[2] is not bolt]
    nut = (
        min(remaining, key=lambda item: item[1])[2]
        if remaining
        else (scored[0][2] if scored else None)
    )
    return bolt, nut


def _eval_hard_custom_thread_pair(
    part: Any, mesh: trimesh.Trimesh, expected: dict[str, float]
) -> dict[str, float]:
    del mesh
    bolt_solid, nut_solid = _select_thread_pair_solids(part, expected)
    if bolt_solid is None or nut_solid is None:
        raise ValueError("could not identify bolt and nut solids")

    bolt_bb = _solid_bbox_metrics(bolt_solid)
    nut_bb = _solid_bbox_metrics(nut_solid)
    bolt_pose = (
        0.25
        * _near_score(bolt_bb["center_x"], float(expected["bolt_center_x_mm"]), 0.25)
        + 0.20
        * _near_score(bolt_bb["center_y"], float(expected["bolt_center_y_mm"]), 0.20)
        + 0.20 * _near_score(bolt_bb["min_z"], float(expected["z_min_mm"]), 0.15)
        + 0.15
        * _near_score(
            bolt_bb["max_z"],
            float(expected["bolt_total_h_mm"] + expected["bolt_head_h_mm"]),
            0.40,
        )
        + 0.20 * _near_score(bolt_bb["size_y"], float(expected["major_d_mm"]), 0.8)
    )
    nut_pose = (
        0.30 * _near_score(nut_bb["center_x"], float(expected["nut_center_x_mm"]), 0.25)
        + 0.20
        * _near_score(nut_bb["center_y"], float(expected["nut_center_y_mm"]), 0.20)
        + 0.20 * _near_score(nut_bb["min_z"], float(expected["z_min_mm"]), 0.15)
        + 0.30 * _near_score(nut_bb["size_z"], float(expected["nut_h_mm"]), 0.25)
    )
    pose = 0.5 * bolt_pose + 0.5 * nut_pose

    bolt_mesh = _localize_mesh(
        _mesh_from_part(bolt_solid),
        float(expected["bolt_center_x_mm"]),
        float(expected["bolt_center_y_mm"]),
    )
    nut_mesh = _localize_mesh(
        _mesh_from_part(nut_solid),
        float(expected["nut_center_x_mm"]),
        float(expected["nut_center_y_mm"]),
    )

    thread_z0 = 0.6
    thread_z1 = thread_z0 + float(expected["thread_length_mm"])
    bolt_sig = _thread_signal_metrics(
        bolt_mesh, thread_z0, thread_z1, float(expected["pitch_mm"]), inner=False
    )
    nut_sig = _thread_signal_metrics(
        nut_mesh,
        0.6,
        min(float(expected["nut_h_mm"]) - 0.5, thread_z1),
        float(expected["pitch_mm"]),
        inner=True,
    )

    bolt_major_d = 2.0 * float(bolt_sig["r_hi"])
    bolt_root_d = 2.0 * float(bolt_sig["r_lo"])
    nut_inner_major_d = 2.0 * float(nut_sig["r_hi"])
    nut_inner_root_d = 2.0 * float(nut_sig["r_lo"])
    bolt_dims = (
        0.28 * _near_score(bolt_major_d, float(expected["major_d_mm"]), 0.40)
        + 0.24 * _near_score(bolt_root_d, float(expected["root_d_mm"]), 0.90)
        + 0.24 * _near_score(bolt_bb["size_x"], float(expected["bolt_head_d_mm"]), 0.60)
        + 0.24
        * _near_score(
            bolt_bb["size_z"],
            float(expected["bolt_total_h_mm"] + expected["bolt_head_h_mm"]),
            0.60,
        )
    )
    nut_dims = (
        0.35
        * _near_score(
            max(nut_bb["size_x"], nut_bb["size_y"]),
            float(expected["nut_outer_d_mm"]),
            0.45,
        )
        + 0.25 * _near_score(nut_bb["size_z"], float(expected["nut_h_mm"]), 0.20)
        + 0.20
        * _near_score(
            nut_inner_major_d,
            float(expected["major_d_mm"] + 2.0 * expected["radial_clearance_mm"]),
            0.50,
        )
        + 0.20
        * _near_score(
            nut_inner_root_d,
            float(expected["root_d_mm"] + 2.0 * expected["radial_clearance_mm"]),
            0.50,
        )
    )

    bolt_pitch_score = max(
        _near_score(float(bolt_sig["pitch_est"]), float(expected["pitch_mm"]), 0.24),
        _near_score(float(bolt_sig["pitch_est"]), float(expected["lead_mm"]), 0.55),
    )
    bolt_thread = (
        0.40 * bolt_pitch_score
        + 0.35 * _clamp01(float(bolt_sig["amp_r"]) / 0.18)
        + 0.25 * _near_score(float(bolt_sig["amp_r"]), 0.27, 0.14)
    )
    nut_thread = (
        0.40
        * _near_score(float(nut_sig["pitch_est"]), float(expected["pitch_mm"]), 0.24)
        + 0.35 * _clamp01(float(nut_sig["amp_r"]) / 0.18)
        + 0.25 * _near_score(float(nut_sig["amp_r"]), 0.27, 0.14)
    )
    clearance = _near_score(
        nut_inner_major_d - bolt_major_d,
        2.0 * float(expected["radial_clearance_mm"]),
        0.30,
    )
    starts = max(1.0, float(expected["starts"]))
    bolt_pitch_est = float(bolt_sig["pitch_est"])
    nut_pitch_est = float(nut_sig["pitch_est"])
    expected_pitch = float(expected["pitch_mm"])
    expected_lead = float(expected["lead_mm"])
    pitch_match = _near_score(bolt_pitch_est, nut_pitch_est, 0.12)
    lead_pitch_match = max(
        _near_score(bolt_pitch_est / starts, nut_pitch_est, 0.18),
        _near_score(bolt_pitch_est, nut_pitch_est * starts, 0.36),
    )
    expected_role_match = max(
        0.5
        * (
            _near_score(bolt_pitch_est, expected_pitch, 0.24)
            + _near_score(nut_pitch_est, expected_pitch, 0.24)
        ),
        0.5
        * (
            _near_score(bolt_pitch_est, expected_lead, 0.55)
            + _near_score(nut_pitch_est, expected_pitch, 0.24)
        ),
        0.5
        * (
            _near_score(bolt_pitch_est / starts, expected_pitch, 0.24)
            + _near_score(nut_pitch_est * starts, expected_lead, 0.50)
        ),
    )
    thread_fit = max(pitch_match, lead_pitch_match, expected_role_match)
    geometry_credit = (pose + bolt_dims + nut_dims) / 3.0
    thread_quality = 0.5 * bolt_thread + 0.5 * nut_thread
    reference_profiles = _custom_thread_reference_profiles()
    bolt_profile_score = _circular_profile_match_score(
        _thread_profile_signature(bolt_mesh, reference_profiles["bolt_z"], inner=False),
        reference_profiles["bolt_profile"],
        tolerance=0.12,
    )
    nut_profile_score = _circular_profile_match_score(
        _thread_profile_signature(nut_mesh, reference_profiles["nut_z"], inner=True),
        reference_profiles["nut_profile"],
        tolerance=0.12,
    )
    profile_task = 0.5 * bolt_profile_score + 0.5 * nut_profile_score
    thread_task = thread_quality * (0.50 + 0.25 * clearance + 0.25 * thread_fit)
    task = 0.85 * profile_task + 0.10 * thread_task + 0.05 * geometry_credit
    return {
        "build_success": 1.0,
        "reward": float(task),
        "task_score": float(task),
        "bolt_pitch_est": float(bolt_sig["pitch_est"]),
        "nut_pitch_est": float(nut_sig["pitch_est"]),
        "bolt_major_d": float(bolt_major_d),
        "bolt_root_d": float(bolt_root_d),
        "nut_inner_major_d": float(nut_inner_major_d),
        "nut_inner_root_d": float(nut_inner_root_d),
        "geometry_credit": float(geometry_credit),
        "thread_quality": float(thread_quality),
        "thread_task": float(thread_task),
        "profile_task": float(profile_task),
        "bolt_profile_score": float(bolt_profile_score),
        "nut_profile_score": float(nut_profile_score),
        "clearance": float(clearance),
        "pitch_match": float(pitch_match),
        "lead_pitch_match": float(lead_pitch_match),
        "expected_role_match": float(expected_role_match),
        "thread_fit": float(thread_fit),
    }


@dataclass(frozen=True)
class _CompoundGearboxSelection:
    input_mesh: trimesh.Trimesh | None
    middle_mesh: trimesh.Trimesh | None
    output_mesh: trimesh.Trimesh | None
    free_components: int
    bridge_components: int
    structure_score: float
    failure_reason: str


def _select_compound_gearbox_components(
    part: Any, expected: dict[str, float]
) -> _CompoundGearboxSelection:
    axle_a = (float(expected["axle_a_x_mm"]), float(expected["axle_a_y_mm"]))
    axle_b = (float(expected["axle_b_x_mm"]), float(expected["axle_b_y_mm"]))
    axle_c = (float(expected["axle_c_x_mm"]), float(expected["axle_c_y_mm"]))
    axle_r = float(expected["axle_radius_mm"])
    z_min = float(expected["z_min_mm"])
    z_max = float(expected["z_max_mm"])

    input_components: list[trimesh.Trimesh] = []
    middle_components: list[trimesh.Trimesh] = []
    output_components: list[trimesh.Trimesh] = []
    free_components: list[trimesh.Trimesh] = []
    bridge_components = 0
    for solid in part.solids():
        component = _mesh_from_part(solid)
        points = np.asarray(component.vertices, dtype=float)
        attached_a = _component_attached_to_axle(
            points, axle_a[0], axle_a[1], axle_r, z_min, z_max
        )
        attached_b = _component_attached_to_axle(
            points, axle_b[0], axle_b[1], axle_r, z_min, z_max
        )
        attached_c = _component_attached_to_axle(
            points, axle_c[0], axle_c[1], axle_r, z_min, z_max
        )
        attached_count = int(attached_a) + int(attached_b) + int(attached_c)
        if attached_count > 1:
            bridge_components += 1
        elif attached_a:
            input_components.append(component)
        elif attached_b:
            middle_components.append(component)
        elif attached_c:
            output_components.append(component)
        else:
            free_components.append(component)

    failure_reason = ""
    structure = 0.0
    if bridge_components > 0:
        failure_reason = "component_bridges_multiple_axles"
    elif not input_components or not middle_components or not output_components:
        failure_reason = "missing_axle_components"
    elif (
        len(input_components) == 1
        and len(middle_components) == 1
        and len(output_components) == 1
    ):
        structure = 1.0
    else:
        structure = 0.65
    return _CompoundGearboxSelection(
        input_mesh=_combine_components(input_components),
        middle_mesh=_combine_components(middle_components),
        output_mesh=_combine_components(output_components),
        free_components=len([c for c in free_components if len(c.faces) >= 64]),
        bridge_components=bridge_components,
        structure_score=structure,
        failure_reason=failure_reason,
    )


def _gear_layer_metrics(
    component: trimesh.Trimesh,
    axle_xy: tuple[float, float],
    z_mid: float,
    expected: dict[str, float],
) -> dict[str, float]:
    local = _localize_mesh(component, axle_xy[0], axle_xy[1])
    outer_r = float(
        np.quantile(
            [
                _radial_hit_radius(local, float(a), z_mid)
                for a in np.linspace(0.0, 2.0 * np.pi, 180, endpoint=False)
            ],
            0.94,
        )
    )
    tooth_count, tooth_amp = _estimate_tooth_count(local, z_mid)
    profile = _gear_profile_metrics(local, z_mid)
    module_est = (
        max(0.1, (2.0 * outer_r) / max(1.0, tooth_count + 2.0))
        if tooth_count >= 3.0
        else 0.0
    )
    pitch_r = (
        max(float(expected["axle_radius_mm"]), outer_r - module_est)
        if module_est > 0.0
        else float(expected["axle_radius_mm"])
    )
    gearness = _clamp01(
        0.30 * _clamp01((tooth_count - 6.0) / 6.0)
        + 0.20 * _clamp01(float(profile["spectral_purity"]) / 0.45)
        + 0.20
        * _clamp01(
            float(profile["tooth_depth_q70_q30"])
            / max(0.35, 0.45 * max(module_est, 0.4))
        )
        + 0.15 * _clamp01(tooth_amp / max(0.08, 0.18 * max(module_est, 0.4)))
        + 0.15
        * _clamp01((outer_r - float(expected["axle_radius_mm"])) / max(1.0, outer_r))
    )
    return {
        "outer_r": float(outer_r),
        "tooth_count": float(tooth_count),
        "module_est": float(module_est),
        "pitch_r": float(pitch_r),
        "gearness": float(gearness),
    }


def _compound_gearbox_sim_transfer_score(sim: dict[str, Any]) -> float:
    continuity = _clamp01(1.0 - float(sim["stop_fraction"]))
    engaged = float(_clamp01(float(sim["engaged"])))
    direction = float(_clamp01(float(sim["direction_score"])))
    speed = float(_clamp01(float(sim["speed_score"]))) * direction
    return 0.25 * engaged + 0.25 * continuity + 0.50 * speed


def _run_compound_gearbox_simulation(
    input_mesh: trimesh.Trimesh,
    middle_mesh: trimesh.Trimesh,
    output_mesh: trimesh.Trimesh,
    expected: dict[str, float],
) -> dict[str, Any]:
    script_path = _task_asset_path("compound_gearbox_functional", "blender_sim.py")
    blender_bin = shutil.which("blender")
    if blender_bin is None:
        raise RuntimeError(
            "missing `blender` executable on PATH; install Blender to run compound gearbox scoring"
        )

    max_faces = int(max(256, expected["max_sim_faces"]))
    axle_a = (float(expected["axle_a_x_mm"]), float(expected["axle_a_y_mm"]))
    axle_b = (float(expected["axle_b_x_mm"]), float(expected["axle_b_y_mm"]))
    axle_c = (float(expected["axle_c_x_mm"]), float(expected["axle_c_y_mm"]))
    z_min = float(expected["axle_min_z_mm"])
    z_max = float(expected["axle_max_z_mm"])
    provenance_dir = _current_scoring_provenance_dir()

    with tempfile.TemporaryDirectory(prefix="cad_compound_gearbox_sim_") as tmpdir:
        tmp = Path(tmpdir)
        gear_a_stl = tmp / "gear_a.stl"
        gear_b_stl = tmp / "gear_b.stl"
        gear_c_stl = tmp / "gear_c.stl"
        render_mp4 = tmp / "candidate.mp4"
        _export_mesh_stl(input_mesh, gear_a_stl, max_faces=max_faces)
        _export_mesh_stl(middle_mesh, gear_b_stl, max_faces=max_faces)
        _export_mesh_stl(output_mesh, gear_c_stl, max_faces=max_faces)

        out_json = tmp / "sim.json"
        cmd = [
            blender_bin,
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
            out_json.as_posix(),
            "--input-rpm",
            str(float(expected["input_rpm"])),
            "--target-output-rpm",
            str(float(expected["target_output_rpm"])),
            "--axle-a-x-mm",
            str(float(axle_a[0])),
            "--axle-a-y-mm",
            str(float(axle_a[1])),
            "--axle-b-x-mm",
            str(float(axle_b[0])),
            "--axle-b-y-mm",
            str(float(axle_b[1])),
            "--axle-c-x-mm",
            str(float(axle_c[0])),
            "--axle-c-y-mm",
            str(float(axle_c[1])),
            "--axle-radius-mm",
            str(float(expected["axle_radius_mm"])),
            "--axle-flat-x-from-center-mm",
            str(float(expected["axle_flat_x_from_center_mm"])),
            "--axle-z-min-mm",
            str(z_min),
            "--axle-z-max-mm",
            str(z_max),
            "--max-sim-faces",
            str(int(max_faces)),
            "--drive-rpm-scale",
            str(float(expected["drive_rpm_scale"])),
            "--drive-mode",
            str(expected["drive_mode"]),
            "--sim-fps",
            str(int(float(expected["sim_fps"]))),
            "--sim-seconds",
            str(float(expected["sim_seconds"])),
            "--rb-substeps",
            str(int(float(expected["rb_substeps"]))),
            "--rb-iterations",
            str(int(float(expected["rb_iterations"]))),
            "--mesh-collision-margin",
            str(float(expected["mesh_collision_margin"])),
            "--voxel-remesh-mm",
            str(float(expected["voxel_remesh_mm"])),
            "--phase-a-deg",
            "0.0",
            "--phase-b-deg",
            "0.0",
            "--phase-c-deg",
            "0.0",
        ]
        if provenance_dir is not None:
            cmd.extend(["--render-mp4", render_mp4.as_posix()])
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(
                f"compound gearbox runner failed with code {proc.returncode}: {detail[:600]}"
            )
        if not out_json.exists():
            raise RuntimeError(
                "compound gearbox simulation did not produce metrics JSON"
            )
        data = json.loads(out_json.read_text(encoding="utf-8"))
        sim_error = str(data["sim_error"]).strip()
        if sim_error:
            raise RuntimeError(sim_error[:800])
        data["phase_b_deg"] = 0.0
        data["phase_c_deg"] = 0.0
        if provenance_dir is not None:
            _copy_scoring_provenance_file(gear_a_stl, "functional_candidate_input.stl")
            _copy_scoring_provenance_file(gear_b_stl, "functional_candidate_middle.stl")
            _copy_scoring_provenance_file(gear_c_stl, "functional_candidate_output.stl")
            _copy_scoring_provenance_file(render_mp4, "functional_candidate.mp4")
            _write_scoring_provenance_text(
                "functional_candidate.stdout.txt", proc.stdout
            )
            _write_scoring_provenance_text(
                "functional_candidate.stderr.txt", proc.stderr
            )
            _write_scoring_provenance_json("functional_candidate.sim.json", data)
        return data


def _run_right_angle_compound_gearbox_simulation(
    input_mesh: trimesh.Trimesh,
    compound_mesh: trimesh.Trimesh,
    output_mesh: trimesh.Trimesh,
    expected: dict[str, float],
) -> dict[str, Any]:
    script_path = _task_asset_path(
        "compound_right_angle_gearbox_reverse", "blender_sim.py"
    )
    blender_bin = shutil.which("blender")
    if blender_bin is None:
        raise RuntimeError(
            "missing `blender` executable on PATH; install Blender to run right-angle gearbox scoring"
        )

    max_faces = int(max(256, expected["max_sim_faces"]))
    provenance_dir = _current_scoring_provenance_dir()
    with tempfile.TemporaryDirectory(prefix="cad_right_angle_compound_sim_") as tmpdir:
        tmp = Path(tmpdir)
        gear_a_stl = tmp / "gear_a.stl"
        gear_b_stl = tmp / "gear_b.stl"
        gear_c_stl = tmp / "gear_c.stl"
        render_mp4 = tmp / "candidate.mp4"
        _export_mesh_stl(input_mesh, gear_a_stl, max_faces=max_faces)
        _export_mesh_stl(compound_mesh, gear_b_stl, max_faces=max_faces)
        _export_mesh_stl(output_mesh, gear_c_stl, max_faces=max_faces)

        out_json = tmp / "sim.json"
        cmd = [
            blender_bin,
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
            out_json.as_posix(),
            "--input-rpm",
            str(float(expected["input_rpm"])),
            "--target-output-rpm",
            str(float(expected["target_output_rpm"])),
            "--input-axle-x-mm",
            str(float(expected["input_axle_x_mm"])),
            "--input-axle-y-mm",
            str(float(expected["input_axle_y_mm"])),
            "--input-axle-z-min-mm",
            str(float(expected["input_axle_z_min_mm"])),
            "--input-axle-z-max-mm",
            str(float(expected["input_axle_z_max_mm"])),
            "--compound-axle-x-min-mm",
            str(float(expected["compound_axle_x_min_mm"])),
            "--compound-axle-x-max-mm",
            str(float(expected["compound_axle_x_max_mm"])),
            "--compound-axle-y-mm",
            str(float(expected["compound_axle_y_mm"])),
            "--compound-axle-z-mm",
            str(float(expected["compound_axle_z_mm"])),
            "--output-axle-x-mm",
            str(float(expected["output_axle_x_mm"])),
            "--output-axle-y-min-mm",
            str(float(expected["output_axle_y_min_mm"])),
            "--output-axle-y-max-mm",
            str(float(expected["output_axle_y_max_mm"])),
            "--output-axle-z-mm",
            str(float(expected["output_axle_z_mm"])),
            "--max-sim-faces",
            str(int(max_faces)),
            "--drive-rpm-scale",
            str(float(expected["drive_rpm_scale"])),
            "--drive-mode",
            str(expected["drive_mode"]),
            "--motor-max-impulse",
            str(float(expected["motor_max_impulse"])),
            "--gear-b-mass-kg",
            str(float(expected["gear_b_mass_kg"])),
            "--gear-c-mass-kg",
            str(float(expected["gear_c_mass_kg"])),
            "--rb-linear-damping",
            str(float(expected["rb_linear_damping"])),
            "--rb-angular-damping",
            str(float(expected["rb_angular_damping"])),
            "--sim-fps",
            str(int(float(expected["sim_fps"]))),
            "--sim-seconds",
            str(float(expected["sim_seconds"])),
            "--rb-substeps",
            str(int(float(expected["rb_substeps"]))),
            "--rb-iterations",
            str(int(float(expected["rb_iterations"]))),
            "--mesh-collision-margin",
            str(float(expected["mesh_collision_margin"])),
            "--voxel-remesh-mm",
            str(float(expected["voxel_remesh_mm"])),
            "--shaft-radius-mm",
            str(float(expected["axle_radius_mm"])),
            "--phase-a-deg",
            "0.0",
            "--phase-b-deg",
            "0.0",
            "--phase-c-deg",
            "0.0",
        ]
        if provenance_dir is not None:
            cmd.extend(["--render-mp4", render_mp4.as_posix()])
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(
                f"right-angle compound gearbox runner failed with code {proc.returncode}: {detail[:600]}"
            )
        if not out_json.exists():
            raise RuntimeError(
                "right-angle compound gearbox simulation did not produce metrics JSON"
            )
        data = json.loads(out_json.read_text(encoding="utf-8"))
        sim_error = str(data["sim_error"]).strip()
        if sim_error:
            raise RuntimeError(sim_error[:800])
        data["phase_b_deg"] = 0.0
        data["phase_c_deg"] = 0.0
        if provenance_dir is not None:
            _copy_scoring_provenance_file(gear_a_stl, "functional_candidate_input.stl")
            _copy_scoring_provenance_file(
                gear_b_stl, "functional_candidate_compound.stl"
            )
            _copy_scoring_provenance_file(gear_c_stl, "functional_candidate_output.stl")
            _copy_scoring_provenance_file(render_mp4, "functional_candidate.mp4")
            _write_scoring_provenance_text(
                "functional_candidate.stdout.txt", proc.stdout
            )
            _write_scoring_provenance_text(
                "functional_candidate.stderr.txt", proc.stderr
            )
            _write_scoring_provenance_json("functional_candidate.sim.json", data)
        return data


def _eval_hard_compound_gearbox(
    part: Any, mesh: trimesh.Trimesh, expected: dict[str, float]
) -> dict[str, float]:
    del mesh
    selection = _select_compound_gearbox_components(part, expected)
    free_penalty = 1.0 / (1.0 + float(selection.free_components))
    axle_a = (float(expected["axle_a_x_mm"]), float(expected["axle_a_y_mm"]))
    axle_b = (float(expected["axle_b_x_mm"]), float(expected["axle_b_y_mm"]))
    axle_c = (float(expected["axle_c_x_mm"]), float(expected["axle_c_y_mm"]))
    axle_fit_a = _axle_fit_score(selection.input_mesh, axle_a, expected)
    axle_fit_b = _axle_fit_score(selection.middle_mesh, axle_b, expected)
    axle_fit_c = _axle_fit_score(selection.output_mesh, axle_c, expected)
    axle_fit = (axle_fit_a + axle_fit_b + axle_fit_c) / 3.0

    if selection.failure_reason:
        return {
            "build_success": 1.0,
            "reward": 0.0,
            "task_score": 0.0,
            "failure_reason": selection.failure_reason,
        }

    if (
        selection.input_mesh is None
        or selection.middle_mesh is None
        or selection.output_mesh is None
    ):
        raise ValueError(
            "compound gearbox selection unexpectedly missing a shaft component"
        )

    z_mid = 0.5 * (float(expected["z_min_mm"]) + float(expected["z_max_mm"]))
    a_mid = _gear_layer_metrics(selection.input_mesh, axle_a, z_mid, expected)
    b_mid = _gear_layer_metrics(selection.middle_mesh, axle_b, z_mid, expected)
    c_mid = _gear_layer_metrics(selection.output_mesh, axle_c, z_mid, expected)
    gearness = (a_mid["gearness"] + b_mid["gearness"] + c_mid["gearness"]) / 3.0

    if axle_fit <= 0.0:
        return {
            "build_success": 1.0,
            "reward": 0.0,
            "task_score": 0.0,
            "failure_reason": "axle_collision",
        }

    sim = _run_compound_gearbox_simulation(
        selection.input_mesh,
        selection.middle_mesh,
        selection.output_mesh,
        expected,
    )
    physical = _compound_gearbox_sim_transfer_score(sim)
    task = physical * selection.structure_score * axle_fit * free_penalty * gearness
    return {
        "build_success": 1.0,
        "reward": float(task),
        "task_score": float(task),
        "failure_reason": "",
    }


def _component_attached_to_axis_line(
    points: np.ndarray,
    axis: str,
    line_a: float,
    line_b: float,
    axle_r: float,
    span_min: float,
    span_max: float,
) -> bool:
    if len(points) == 0:
        return False
    if axis == "x":
        axis_values = points[:, 0]
        radial = np.linalg.norm(
            points[:, 1:3] - np.array([line_a, line_b], dtype=float), axis=1
        )
    elif axis == "y":
        axis_values = points[:, 1]
        radial = np.linalg.norm(
            points[:, [0, 2]] - np.array([line_a, line_b], dtype=float), axis=1
        )
    elif axis == "z":
        axis_values = points[:, 2]
        radial = np.linalg.norm(
            points[:, :2] - np.array([line_a, line_b], dtype=float), axis=1
        )
    else:
        raise ValueError(f"unsupported axis: {axis}")
    if (
        float(np.max(axis_values)) < span_min + 0.2
        or float(np.min(axis_values)) > span_max - 0.2
    ):
        return False
    near_axle = float(np.quantile(radial, 0.03)) <= axle_r + 0.95
    has_body = float(np.quantile(radial, 0.97)) >= axle_r + 1.8
    return bool(near_axle and has_body)


def _component_axis_from_bbox(bbox: dict[str, float]) -> str:
    return min(("x", "y", "z"), key=lambda axis: float(bbox[f"size_{axis}"]))


@dataclass(frozen=True)
class _RightAngleGearboxSelection:
    input_solids: tuple[Any, ...]
    compound_solids: tuple[Any, ...]
    output_solids: tuple[Any, ...]
    free_components: int
    bridge_components: int
    structure_score: float
    failure_reason: str


def _select_right_angle_gearbox_solids(
    part: Any, expected: dict[str, float]
) -> _RightAngleGearboxSelection:
    axle_r = float(expected["axle_radius_mm"])
    input_solids: list[Any] = []
    compound_solids: list[Any] = []
    output_solids: list[Any] = []
    free_components = 0
    bridge_components = 0

    for solid in part.solids():
        component = _mesh_from_part(solid)
        points = np.asarray(component.vertices, dtype=float)
        inferred_axis = _component_axis_from_bbox(_solid_bbox_metrics(solid))
        attached_input = _component_attached_to_axis_line(
            points,
            "z",
            float(expected["input_axle_x_mm"]),
            float(expected["input_axle_y_mm"]),
            axle_r,
            float(expected["input_axle_z_min_mm"]),
            float(expected["input_axle_z_max_mm"]),
        )
        attached_compound = _component_attached_to_axis_line(
            points,
            "x",
            float(expected["compound_axle_y_mm"]),
            float(expected["compound_axle_z_mm"]),
            axle_r,
            float(expected["compound_axle_x_min_mm"]),
            float(expected["compound_axle_x_max_mm"]),
        )
        attached_output = _component_attached_to_axis_line(
            points,
            "y",
            float(expected["output_axle_x_mm"]),
            float(expected["output_axle_z_mm"]),
            axle_r,
            float(expected["output_axle_y_min_mm"]),
            float(expected["output_axle_y_max_mm"]),
        )
        matches: list[str] = []
        if attached_input and inferred_axis == "z":
            matches.append("input")
        if attached_compound and inferred_axis == "x":
            matches.append("compound")
        if attached_output and inferred_axis == "y":
            matches.append("output")
        if len(matches) > 1:
            bridge_components += 1
        elif matches == ["input"]:
            input_solids.append(solid)
        elif matches == ["compound"]:
            compound_solids.append(solid)
        elif matches == ["output"]:
            output_solids.append(solid)
        elif attached_input or attached_compound or attached_output:
            bridge_components += 1
        else:
            free_components += 1

    failure_reason = ""
    if bridge_components > 0:
        failure_reason = "component_bridges_multiple_axes"
    elif len(input_solids) != 1 or len(output_solids) != 1 or len(compound_solids) < 1:
        failure_reason = "missing_axis_components"

    structure_score = 0.0
    if not failure_reason:
        if len(compound_solids) == 2:
            structure_score = 1.0
        elif len(compound_solids) == 1:
            structure_score = 0.65
        else:
            structure_score = 0.45

    return _RightAngleGearboxSelection(
        input_solids=tuple(input_solids),
        compound_solids=tuple(compound_solids),
        output_solids=tuple(output_solids),
        free_components=free_components,
        bridge_components=bridge_components,
        structure_score=structure_score,
        failure_reason=failure_reason,
    )



def _eval_hard_compound_right_angle_gearbox(
    part: Any, mesh: trimesh.Trimesh, expected: dict[str, float]
) -> dict[str, float]:
    del mesh
    selection = _select_right_angle_gearbox_solids(part, expected)
    if selection.failure_reason:
        return {
            "build_success": 1.0,
            "reward": 0.0,
            "task_score": 0.0,
            "failure_reason": selection.failure_reason,
        }

    input_solid = selection.input_solids[0]
    output_solid = selection.output_solids[0]
    stage_solids = [input_solid, *selection.compound_solids, output_solid]
    intersecting_pairs = 0
    for idx in range(len(stage_solids)):
        for jdx in range(idx + 1, len(stage_solids)):
            intersection = stage_solids[idx].intersect(stage_solids[jdx])
            volume = (
                float(getattr(intersection, "volume", 0.0))
                if intersection is not None
                else 0.0
            )
            if volume > 1e-4:
                intersecting_pairs += 1
                break
        if intersecting_pairs > 0:
            break
    free_penalty = 1.0 / (1.0 + float(selection.free_components))

    if intersecting_pairs > 0:
        return {
            "build_success": 1.0,
            "reward": 0.0,
            "task_score": 0.0,
            "failure_reason": "intersecting_bodies",
        }

    input_mesh = _mesh_from_part(input_solid)
    compound_mesh = _combine_components(
        [_mesh_from_part(solid) for solid in selection.compound_solids]
    )
    output_mesh = _mesh_from_part(output_solid)
    if compound_mesh is None:
        raise ValueError(
            "right-angle compound selection unexpectedly missing compound mesh"
        )

    sim = _run_right_angle_compound_gearbox_simulation(
        input_mesh, compound_mesh, output_mesh, expected
    )
    physical = _compound_gearbox_sim_transfer_score(sim)
    task = physical * selection.structure_score * free_penalty
    return {
        "build_success": 1.0,
        "task_score": float(task),
        "reward": float(task),
        "failure_reason": "",
    }


@dataclass(frozen=True)
class _GearboxSelection:
    input_mesh: trimesh.Trimesh | None
    output_mesh: trimesh.Trimesh | None
    free_components: int
    bridge_components: int
    structure_score: float
    failure_reason: str


def _combine_components(components: list[trimesh.Trimesh]) -> trimesh.Trimesh | None:
    if not components:
        return None
    if len(components) == 1:
        return components[0].copy()
    combined = trimesh.util.concatenate(components)
    if not isinstance(combined, trimesh.Trimesh):
        raise ValueError("failed to combine gearbox components")
    return combined


def _select_gearbox_components(
    part: Any, expected: dict[str, float]
) -> _GearboxSelection:
    axle_a = (float(expected["axle_a_x_mm"]), float(expected["axle_a_y_mm"]))
    axle_b = (float(expected["axle_b_x_mm"]), float(expected["axle_b_y_mm"]))
    axle_r = float(expected["axle_radius_mm"])
    z_min = float(expected["z_min_mm"])
    z_max = float(expected["z_max_mm"])

    input_components: list[trimesh.Trimesh] = []
    output_components: list[trimesh.Trimesh] = []
    free_components: list[trimesh.Trimesh] = []
    bridge_components = 0
    for solid in part.solids():
        component = _mesh_from_part(solid)
        points = np.asarray(component.vertices, dtype=float)
        attached_a = _component_attached_to_axle(
            points, axle_a[0], axle_a[1], axle_r, z_min, z_max
        )
        attached_b = _component_attached_to_axle(
            points, axle_b[0], axle_b[1], axle_r, z_min, z_max
        )
        if attached_a and attached_b:
            bridge_components += 1
        elif attached_a:
            input_components.append(component)
        elif attached_b:
            output_components.append(component)
        else:
            free_components.append(component)

    structure = 0.0
    failure_reason = ""
    if bridge_components > 0:
        failure_reason = "component_bridges_both_axles"
    elif not input_components or not output_components:
        failure_reason = "missing_axle_components"
    elif len(input_components) == 1 and len(output_components) == 1:
        structure = 1.0
    else:
        structure = 0.65

    return _GearboxSelection(
        input_mesh=_combine_components(input_components),
        output_mesh=_combine_components(output_components),
        free_components=len([c for c in free_components if len(c.faces) >= 64]),
        bridge_components=bridge_components,
        structure_score=structure,
        failure_reason=failure_reason,
    )


def _prepare_mesh_for_sim(
    component: trimesh.Trimesh, max_faces: int
) -> trimesh.Trimesh:
    prepared = _repair_mesh_for_sim(component)
    prepared = _downsample_mesh_faces(prepared, max_faces=max_faces)
    if len(prepared.vertices) < 16 or len(prepared.faces) < 32:
        raise ValueError("prepared simulation mesh is too small")
    return prepared


def _export_mesh_stl(
    component: trimesh.Trimesh, out_path: Path, max_faces: int
) -> None:
    prepared = _prepare_mesh_for_sim(component, max_faces=max_faces)
    stl = prepared.export(file_type="stl")
    if isinstance(stl, str):
        data = stl.encode("utf-8")
    else:
        data = bytes(stl)
    out_path.write_bytes(data)
    if out_path.stat().st_size <= 128:
        raise ValueError(f"empty STL export for {out_path.name}")


def _gearbox_phase_trials(output_teeth_est: float) -> list[float]:
    if output_teeth_est < 3.0:
        return [0.0]
    tooth_pitch_deg = 360.0 / float(output_teeth_est)
    trials = [
        0.0,
        0.25 * tooth_pitch_deg,
        0.5 * tooth_pitch_deg,
        0.75 * tooth_pitch_deg,
    ]
    seen: set[float] = set()
    out: list[float] = []
    for phase_deg in trials:
        rounded = round(float(phase_deg), 6)
        if rounded in seen:
            continue
        seen.add(rounded)
        out.append(float(phase_deg))
    return out or [0.0]


def _gearbox_sim_transfer_score(sim: dict[str, Any]) -> float:
    continuity = _clamp01(1.0 - float(sim["stop_fraction"]))
    return _clamp01(
        0.20 * float(sim["engaged"])
        + 0.20 * float(sim["direction_score"])
        + 0.25 * float(sim["speed_score"])
        + 0.20 * float(sim["ratio_score"])
        + 0.15 * continuity
    )


def _run_gearbox_simulation(
    input_mesh: trimesh.Trimesh,
    output_mesh: trimesh.Trimesh,
    expected: dict[str, float],
    output_teeth_est: float,
) -> dict[str, Any]:
    script_path = _task_asset_path(
        "gearbox_functional_collision_trim", "blender_sim.py"
    )
    blender_bin = shutil.which("blender")
    if blender_bin is None:
        raise RuntimeError(
            "missing `blender` executable on PATH; install Blender to run gearbox scoring"
        )

    max_faces = int(max(256, expected["max_sim_faces"]))
    provenance_dir = _current_scoring_provenance_dir()
    with tempfile.TemporaryDirectory(prefix="cad_gearbox_sim_") as tmpdir:
        tmp = Path(tmpdir)
        gear_a_stl = tmp / "gear_a.stl"
        gear_b_stl = tmp / "gear_b.stl"
        _export_mesh_stl(input_mesh, gear_a_stl, max_faces=max_faces)
        _export_mesh_stl(output_mesh, gear_b_stl, max_faces=max_faces)

        best: dict[str, Any] | None = None
        best_phase = 0.0
        best_score = -1.0
        last_error = ""
        last_stdout = ""
        last_stderr = ""

        for idx, phase_b_deg in enumerate(_gearbox_phase_trials(output_teeth_est)):
            out_json = tmp / f"sim_{idx}.json"
            cmd = [
                blender_bin,
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
                out_json.as_posix(),
                "--input-rpm",
                str(float(expected["input_rpm"])),
                "--target-output-rpm",
                str(float(expected["target_output_rpm"])),
                "--axle-a-x-mm",
                str(float(expected["axle_a_x_mm"])),
                "--axle-a-y-mm",
                str(float(expected["axle_a_y_mm"])),
                "--axle-b-x-mm",
                str(float(expected["axle_b_x_mm"])),
                "--axle-b-y-mm",
                str(float(expected["axle_b_y_mm"])),
                "--axle-radius-mm",
                str(float(expected["axle_radius_mm"])),
                "--axle-flat-x-from-center-mm",
                str(float(expected["axle_flat_x_from_center_mm"])),
                "--axle-z-min-mm",
                str(float(expected["axle_min_z_mm"])),
                "--axle-z-max-mm",
                str(float(expected["axle_max_z_mm"])),
                "--max-sim-faces",
                str(max_faces),
                "--drive-rpm-scale",
                str(float(expected["drive_rpm_scale"])),
                "--sim-fps",
                str(int(float(expected["sim_fps"]))),
                "--sim-seconds",
                str(float(expected["sim_seconds"])),
                "--rb-substeps",
                str(int(float(expected["rb_substeps"]))),
                "--rb-iterations",
                str(int(float(expected["rb_iterations"]))),
                "--voxel-remesh-mm",
                str(float(expected["voxel_remesh_mm"])),
                "--phase-a-deg",
                "0.0",
                "--phase-b-deg",
                f"{phase_b_deg:.6f}",
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()
                last_error = f"gearbox simulation runner failed with code {proc.returncode}: {detail[:600]}"
                continue
            if not out_json.exists():
                last_error = "gearbox simulation did not produce metrics JSON"
                continue
            data = json.loads(out_json.read_text(encoding="utf-8"))
            sim_error = str(data["sim_error"]).strip()
            if sim_error:
                last_error = sim_error[:800]
                continue
            data["phase_b_deg"] = float(phase_b_deg)
            score = _gearbox_sim_transfer_score(data)
            if score > best_score:
                best = data
                best_phase = float(phase_b_deg)
                best_score = score
                last_stdout = proc.stdout
                last_stderr = proc.stderr

        if best is None:
            raise RuntimeError(
                last_error or "all gearbox simulation phase trials failed"
            )
        if provenance_dir is not None:
            final_json = tmp / "sim_final.json"
            render_mp4 = tmp / "candidate.mp4"
            render_cmd = [
                blender_bin,
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
                final_json.as_posix(),
                "--render-mp4",
                render_mp4.as_posix(),
                "--input-rpm",
                str(float(expected["input_rpm"])),
                "--target-output-rpm",
                str(float(expected["target_output_rpm"])),
                "--axle-a-x-mm",
                str(float(expected["axle_a_x_mm"])),
                "--axle-a-y-mm",
                str(float(expected["axle_a_y_mm"])),
                "--axle-b-x-mm",
                str(float(expected["axle_b_x_mm"])),
                "--axle-b-y-mm",
                str(float(expected["axle_b_y_mm"])),
                "--axle-radius-mm",
                str(float(expected["axle_radius_mm"])),
                "--axle-flat-x-from-center-mm",
                str(float(expected["axle_flat_x_from_center_mm"])),
                "--axle-z-min-mm",
                str(float(expected["axle_min_z_mm"])),
                "--axle-z-max-mm",
                str(float(expected["axle_max_z_mm"])),
                "--max-sim-faces",
                str(max_faces),
                "--drive-rpm-scale",
                str(float(expected["drive_rpm_scale"])),
                "--sim-fps",
                str(int(float(expected["sim_fps"]))),
                "--sim-seconds",
                str(float(expected["sim_seconds"])),
                "--rb-substeps",
                str(int(float(expected["rb_substeps"]))),
                "--rb-iterations",
                str(int(float(expected["rb_iterations"]))),
                "--voxel-remesh-mm",
                str(float(expected["voxel_remesh_mm"])),
                "--phase-a-deg",
                "0.0",
                "--phase-b-deg",
                f"{best_phase:.6f}",
            ]
            render_proc = subprocess.run(
                render_cmd,
                capture_output=True,
                text=True,
                check=False,
            )
            if render_proc.returncode == 0 and final_json.exists():
                rendered = json.loads(final_json.read_text(encoding="utf-8"))
                rendered["phase_b_deg"] = best_phase
                _write_scoring_provenance_json(
                    "functional_candidate.sim.json", rendered
                )
                _write_scoring_provenance_text(
                    "functional_candidate.stdout.txt", render_proc.stdout
                )
                _write_scoring_provenance_text(
                    "functional_candidate.stderr.txt", render_proc.stderr
                )
                _copy_scoring_provenance_file(
                    render_mp4, "functional_candidate.mp4"
                )
            else:
                _write_scoring_provenance_json("functional_candidate.sim.json", best)
                _write_scoring_provenance_text(
                    "functional_candidate.stdout.txt", last_stdout
                )
                _write_scoring_provenance_text(
                    "functional_candidate.stderr.txt", last_stderr
                )
            _copy_scoring_provenance_file(gear_a_stl, "functional_candidate_input.stl")
            _copy_scoring_provenance_file(
                gear_b_stl, "functional_candidate_output.stl"
            )
        return best


def _axle_fit_score(
    component: trimesh.Trimesh | None,
    axle_xy: tuple[float, float],
    expected: dict[str, float],
) -> float:
    if component is None:
        return 0.0
    axle_r = float(expected["axle_radius_mm"])
    flat_x = float(expected["axle_flat_x_from_center_mm"])
    if _mesh_collides_axle(
        component,
        axle_xy[0],
        axle_xy[1],
        axle_r,
        float(expected["axle_min_z_mm"]),
        float(expected["axle_max_z_mm"]),
        flat_x,
    ):
        return 0.0
    local = _localize_mesh(component, axle_xy[0], axle_xy[1])
    z_mid = 0.5 * (float(expected["axle_min_z_mm"]) + float(expected["axle_max_z_mm"]))
    profile_scores: list[float] = []
    for angle in np.linspace(0.0, 2.0 * np.pi, 24, endpoint=False):
        measured = _radial_first_hit_radius(local, float(angle), z_mid)
        if measured <= 1e-6:
            profile_scores.append(0.0)
            continue
        target = _d_shaft_profile_radius(float(angle), axle_r, flat_x)
        profile_scores.append(_near_score(measured, target, 0.20))
    return float(np.mean(profile_scores)) if profile_scores else 0.0


def _gear_component_metrics(
    component: trimesh.Trimesh,
    axle_xy: tuple[float, float],
    expected: dict[str, float],
) -> dict[str, float]:
    local = _localize_mesh(component, axle_xy[0], axle_xy[1])
    bbox = _bbox_metrics(local)
    axle_r = float(expected["axle_radius_mm"])
    z_mid = float(
        np.clip(0.5 * (bbox["min_z"] + bbox["max_z"]), bbox["min_z"], bbox["max_z"])
    )

    outer_r = max(0.0, 0.5 * _estimate_outer_d(local))
    bore_r = max(
        axle_r, 0.5 * _estimate_bore_d(local, max_r=max(axle_r + 2.5, outer_r * 0.45))
    )
    tooth_count, tooth_amp = _estimate_tooth_count(local, z_mid)
    profile = _gear_profile_metrics(local, z_mid)
    coverage = _axle_angular_coverage(
        np.asarray(local.vertices, dtype=float),
        0.0,
        0.0,
        max(axle_r + 1.5, outer_r - 1.0),
        axle_r,
    )

    if tooth_count >= 3.0 and outer_r > axle_r + 0.5:
        module_est = max(0.1, (2.0 * outer_r) / max(1.0, tooth_count + 2.0))
        pitch_r = max(axle_r, outer_r - module_est)
    else:
        module_est = 0.0
        pitch_r = axle_r

    tooth_depth = float(profile["tooth_depth_q70_q30"])
    spectral_score = _clamp01((float(profile["spectral_purity"]) - 0.12) / 0.30)
    tooth_count_score = _clamp01((tooth_count - 6.0) / 8.0)
    tooth_depth_score = _clamp01(tooth_depth / max(0.35, 0.45 * max(module_est, 0.5)))
    tooth_amp_score = _clamp01(tooth_amp / max(0.08, 0.18 * max(module_est, 0.5)))
    annulus_score = _clamp01((outer_r - bore_r) / max(1.0, outer_r))
    z_score = 0.5 * _near_score(
        bbox["min_z"], float(expected["z_min_mm"]), 0.20
    ) + 0.5 * _near_score(bbox["max_z"], float(expected["z_max_mm"]), 0.25)
    gearness = _clamp01(
        0.22 * coverage
        + 0.22 * spectral_score
        + 0.18 * tooth_depth_score
        + 0.14 * tooth_amp_score
        + 0.12 * tooth_count_score
        + 0.07 * annulus_score
        + 0.05 * z_score
    )

    return {
        "outer_r": float(outer_r),
        "bore_r": float(bore_r),
        "pitch_r": float(pitch_r),
        "module_est": float(module_est),
        "tooth_count": float(tooth_count),
        "tooth_amp": float(tooth_amp),
        "gearness": float(gearness),
        "coverage": float(coverage),
        "spectral_purity": float(profile["spectral_purity"]),
        "tooth_depth": float(tooth_depth),
    }


def _eval_hard_gearbox(
    part: Any, mesh: trimesh.Trimesh, expected: dict[str, float]
) -> dict[str, float]:
    del mesh
    selection = _select_gearbox_components(part, expected)
    extra_penalty = 1.0 / (1.0 + float(selection.free_components))
    axle_a = (float(expected["axle_a_x_mm"]), float(expected["axle_a_y_mm"]))
    axle_b = (float(expected["axle_b_x_mm"]), float(expected["axle_b_y_mm"]))
    axle_fit_input = _axle_fit_score(selection.input_mesh, axle_a, expected)
    axle_fit_output = _axle_fit_score(selection.output_mesh, axle_b, expected)
    axle_fit = 0.5 * axle_fit_input + 0.5 * axle_fit_output

    if selection.failure_reason:
        return {
            "build_success": 1.0,
            "reward": 0.0,
            "task_score": 0.0,
            "direction_score": 0.0,
            "speed_score": 0.0,
            "engaged": 0.0,
            "axle_fit_input": float(axle_fit_input),
            "axle_fit_output": float(axle_fit_output),
            "extra_penalty": float(extra_penalty),
            "bridge_components": float(selection.bridge_components),
            "failure_reason": selection.failure_reason,
            "simulation_error": "",
        }

    if selection.input_mesh is None or selection.output_mesh is None:
        raise ValueError("gearbox selection unexpectedly missing axle components")

    input_metrics = _gear_component_metrics(selection.input_mesh, axle_a, expected)
    output_metrics = _gear_component_metrics(selection.output_mesh, axle_b, expected)
    gearness = 0.5 * input_metrics["gearness"] + 0.5 * output_metrics["gearness"]

    if axle_fit <= 0.0:
        return {
            "build_success": 1.0,
            "reward": 0.0,
            "task_score": 0.0,
            "direction_score": 0.0,
            "speed_score": 0.0,
            "engaged": 0.0,
            "continuity_score": 0.0,
            "axle_fit_input": float(axle_fit_input),
            "axle_fit_output": float(axle_fit_output),
            "input_teeth_est": float(input_metrics["tooth_count"]),
            "output_teeth_est": float(output_metrics["tooth_count"]),
            "input_module_est": float(input_metrics["module_est"]),
            "output_module_est": float(output_metrics["module_est"]),
            "extra_penalty": float(extra_penalty),
            "bridge_components": float(selection.bridge_components),
            "failure_reason": "axle_collision",
            "simulation_error": "",
        }

    sim = _run_gearbox_simulation(
        selection.input_mesh,
        selection.output_mesh,
        expected,
        output_teeth_est=output_metrics["tooth_count"],
    )

    direction_score = float(_clamp01(float(sim["direction_score"])))
    speed_score_raw = float(_clamp01(float(sim["speed_score"])))
    speed_score = speed_score_raw * direction_score
    engaged = float(_clamp01(float(sim["engaged"])))
    continuity_score = _clamp01(1.0 - float(sim["stop_fraction"]))
    physical = 0.25 * engaged + 0.25 * continuity_score + 0.50 * speed_score
    geometry_factor = axle_fit * selection.structure_score * gearness * extra_penalty
    task = geometry_factor * physical
    return {
        "build_success": 1.0,
        "reward": float(task),
        "task_score": float(task),
        "direction_score": direction_score,
        "speed_score": speed_score,
        "speed_score_raw": float(speed_score_raw),
        "engaged": engaged,
        "continuity_score": float(continuity_score),
        "axle_fit_input": float(axle_fit_input),
        "axle_fit_output": float(axle_fit_output),
        "input_teeth_est": float(input_metrics["tooth_count"]),
        "output_teeth_est": float(output_metrics["tooth_count"]),
        "input_module_est": float(input_metrics["module_est"]),
        "output_module_est": float(output_metrics["module_est"]),
        "extra_penalty": float(extra_penalty),
        "bridge_components": float(selection.bridge_components),
        "output_rpm": float(sim["output_rpm"]),
        "input_rpm_measured": float(sim["input_rpm_measured"]),
        "phase_b_deg": float(sim["phase_b_deg"]),
        "stop_fraction": float(sim["stop_fraction"]),
        "failure_reason": "",
        "simulation_error": "",
    }


def _eval_cube(mesh: trimesh.Trimesh, expected: dict[str, float]) -> dict[str, float]:
    bbox = _bbox_metrics(mesh)
    pose = (
        0.30 * _near_score(bbox["max_z"], expected["top_z_mm"], 0.05)
        + 0.25 * _near_score(bbox["min_z"], expected["min_z_mm"], 0.15)
        + 0.225 * _near_score(bbox["center_x"], expected["center_x_mm"], 0.12)
        + 0.225 * _near_score(bbox["center_y"], expected["center_y_mm"], 0.12)
    )
    dims = (
        _near_score(bbox["size_x"], expected["side_mm"], 0.15)
        + _near_score(bbox["size_y"], expected["side_mm"], 0.15)
        + _near_score(bbox["size_z"], expected["side_mm"], 0.15)
    ) / 3.0
    shape = _clamp01(
        1.0
        - (abs(bbox["size_x"] - bbox["size_y"]) + abs(bbox["size_y"] - bbox["size_z"]))
        / 1.0
    )
    composite = _conservative_composite(
        pose, dims, shape, pose_weight=0.4, dims_weight=0.35, task_weight=0.25
    )
    return {
        "build_success": 1.0,
        "composite": _clamp01(composite),
        "pose_score": _clamp01(pose),
        "geometry_score": _clamp01((pose + dims) / 2.0),
        "task_score": _clamp01(shape),
        "task_specific_score": _clamp01(shape),
    }


def _eval_extrusion(
    mesh: trimesh.Trimesh, expected: dict[str, float]
) -> dict[str, float]:
    bbox = _bbox_metrics(mesh)
    pose = (
        0.30 * _near_score(bbox["min_z"], expected["min_z_mm"], 0.08)
        + 0.30 * _near_score(bbox["max_z"], expected["top_z_mm"], 0.12)
        + 0.20 * _near_score(bbox["center_x"], 0.0, 0.12)
        + 0.20 * _near_score(bbox["center_y"], 0.0, 0.12)
    )
    dims = (
        0.3 * _near_score(bbox["size_x"], expected["width_mm"], 0.18)
        + 0.3 * _near_score(bbox["size_y"], expected["depth_mm"], 0.18)
        + 0.4 * _near_score(bbox["size_z"], expected["length_mm"], 0.2)
    )

    hole_centers = [(0.0, 0.0), (7.0, 0.0), (-7.0, 0.0), (0.0, 7.0), (0.0, -7.0)]
    solid_probes = [(9.0, 9.0), (-9.0, 9.0), (9.0, -9.0), (-9.0, -9.0)]

    open_scores = []
    for x, y in hole_centers:
        n_hits = len(_z_hits(mesh, x, y))
        open_scores.append(1.0 if n_hits == 0 else 0.0)

    solid_scores = []
    for x, y in solid_probes:
        n_hits = len(_z_hits(mesh, x, y))
        solid_scores.append(1.0 if n_hits >= 2 else 0.0)

    holes = 0.7 * float(np.mean(open_scores)) + 0.3 * float(np.mean(solid_scores))
    composite = _conservative_composite(
        pose, dims, holes, pose_weight=0.35, dims_weight=0.35, task_weight=0.30
    )
    return {
        "build_success": 1.0,
        "composite": _clamp01(composite),
        "pose_score": _clamp01(pose),
        "geometry_score": _clamp01((pose + dims) / 2.0),
        "task_score": _clamp01(holes),
        "task_specific_score": _clamp01(holes),
    }


def _eval_gear(mesh: trimesh.Trimesh, expected: dict[str, float]) -> dict[str, float]:
    bbox = _bbox_metrics(mesh)
    outer_d = _estimate_outer_d(mesh)
    bore_d = _estimate_bore_d(mesh, max_r=4.0)
    z_mid = float((bbox["max_z"] + bbox["min_z"]) / 2.0)
    tooth_count_est, tooth_amp = _estimate_tooth_count(mesh, z_mid)
    profile = _gear_profile_metrics(mesh, z_mid)

    pose = (
        0.35 * _near_score(bbox["max_z"], expected["top_z_mm"], 0.05)
        + 0.35 * _near_score(bbox["min_z"], expected["min_z_mm"], 0.1)
        + 0.15 * _near_score(bbox["center_x"], 0.0, 0.10)
        + 0.15 * _near_score(bbox["center_y"], 0.0, 0.10)
    )
    dims = (
        0.45 * _near_score(outer_d, expected["outer_d_mm"], 0.35)
        + 0.35 * _near_score(bbox["size_z"], expected["thickness_mm"], 0.15)
        + 0.20 * _near_score(bore_d, expected["bore_d_mm"], 0.4)
    )
    root_r_expected = expected["root_d_mm"] / 2.0
    tooth_depth_expected = (expected["outer_d_mm"] - expected["root_d_mm"]) / 2.0
    tooth_amp_expected = 0.47 * tooth_depth_expected
    teeth = (
        0.20 * _near_score(tooth_count_est, expected["teeth"], 1.0)
        + 0.10 * _near_score(tooth_amp, tooth_amp_expected, 0.25)
        + 0.30 * _near_score(profile["root_radius_q30"], root_r_expected, 0.35)
        + 0.30 * _near_score(profile["tooth_depth_q70_q30"], tooth_depth_expected, 0.45)
        + 0.10 * _near_score(profile["spectral_purity"], 0.60, 0.20)
    )
    composite = _conservative_composite(
        pose, dims, teeth, pose_weight=0.22, dims_weight=0.28, task_weight=0.50
    )
    return {
        "build_success": 1.0,
        "composite": _clamp01(composite),
        "pose_score": _clamp01(pose),
        "geometry_score": _clamp01((pose + dims) / 2.0),
        "task_score": _clamp01(teeth),
        "task_specific_score": _clamp01(teeth),
    }


def _eval_easy_box(
    mesh: trimesh.Trimesh, expected: dict[str, float]
) -> dict[str, float]:
    bbox = _bbox_metrics(mesh)
    pose = (
        0.30 * _near_score(bbox["min_z"], expected["min_z_mm"], 0.05)
        + 0.30 * _near_score(bbox["max_z"], expected["max_z_mm"], 0.08)
        + 0.20 * _near_score(bbox["center_x"], expected["center_x_mm"], 0.10)
        + 0.20 * _near_score(bbox["center_y"], expected["center_y_mm"], 0.10)
    )
    dims = (
        0.34 * _near_score(bbox["size_x"], expected["size_x_mm"], 0.15)
        + 0.33 * _near_score(bbox["size_y"], expected["size_y_mm"], 0.15)
        + 0.33 * _near_score(bbox["size_z"], expected["size_z_mm"], 0.10)
    )
    shape = 1.0
    composite = _conservative_composite(
        pose, dims, shape, pose_weight=0.48, dims_weight=0.52, task_weight=0.0
    )
    return {
        "build_success": 1.0,
        "composite": _clamp01(composite),
        "pose_score": _clamp01(pose),
        "geometry_score": _clamp01((pose + dims) / 2.0),
        "task_score": _clamp01(shape),
        "task_specific_score": _clamp01(shape),
    }


def _eval_medium_l_bracket(
    mesh: trimesh.Trimesh, expected: dict[str, float]
) -> dict[str, float]:
    bbox = _bbox_metrics(mesh)
    pose = (
        0.30 * _near_score(bbox["min_z"], expected["min_z_mm"], 0.05)
        + 0.30 * _near_score(bbox["max_z"], expected["max_z_mm"], 0.08)
        + 0.20 * _near_score(bbox["min_x"], 0.0, 0.08)
        + 0.20 * _near_score(bbox["min_y"], 0.0, 0.08)
    )
    dims = (
        0.34 * _near_score(bbox["size_x"], expected["bbox_x_mm"], 0.15)
        + 0.33 * _near_score(bbox["size_y"], expected["bbox_y_mm"], 0.15)
        + 0.33 * _near_score(bbox["size_z"], expected["thickness_mm"], 0.10)
    )

    hole_centers = [(10.0, 10.0), (30.0, 10.0)]
    hole_scores = []
    for x, y in hole_centers:
        hole_scores.append(1.0 if len(_z_hits(mesh, x, y)) == 0 else 0.0)

    notch_empty = 1.0 if len(_z_hits(mesh, 30.0, 30.0)) == 0 else 0.0
    solid_probe = 1.0 if len(_z_hits(mesh, 10.0, 30.0)) >= 2 else 0.0

    features = (
        0.45 * float(np.mean(hole_scores)) + 0.30 * notch_empty + 0.25 * solid_probe
    )
    task = features
    composite = _conservative_composite(
        pose, dims, task, pose_weight=0.30, dims_weight=0.30, task_weight=0.40
    )
    return {
        "build_success": 1.0,
        "composite": _clamp01(composite),
        "pose_score": _clamp01(pose),
        "geometry_score": _clamp01((pose + dims) / 2.0),
        "task_score": _clamp01(task),
        "task_specific_score": _clamp01(features),
    }


def _eval_hard_flange(
    mesh: trimesh.Trimesh, expected: dict[str, float]
) -> dict[str, float]:
    bbox = _bbox_metrics(mesh)
    pose = (
        0.28 * _near_score(bbox["min_z"], expected["min_z_mm"], 0.06)
        + 0.28 * _near_score(bbox["max_z"], expected["max_z_mm"], 0.08)
        + 0.22 * _near_score(bbox["center_x"], 0.0, 0.10)
        + 0.22 * _near_score(bbox["center_y"], 0.0, 0.10)
    )
    dims = (
        0.35 * _near_score(bbox["size_x"], expected["base_d_mm"], 0.25)
        + 0.35 * _near_score(bbox["size_y"], expected["base_d_mm"], 0.25)
        + 0.30 * _near_score(bbox["size_z"], expected["height_mm"], 0.12)
    )

    base_r = np.mean(
        [
            _radial_hit_radius(mesh, ang, 4.0)
            for ang in (0.0, np.pi / 2, np.pi, 3 * np.pi / 2)
        ]
    )
    boss_r = np.mean(
        [
            _radial_hit_radius(mesh, ang, 16.0)
            for ang in (0.0, np.pi / 2, np.pi, 3 * np.pi / 2)
        ]
    )
    profile = 0.5 * _near_score(
        2.0 * base_r, expected["base_d_mm"], 0.45
    ) + 0.5 * _near_score(2.0 * boss_r, expected["boss_d_mm"], 0.45)

    center_bore_open = 1.0 if len(_z_hits(mesh, 0.0, 0.0)) == 0 else 0.0
    bolt_scores = []
    for x, y in ((15.0, 0.0), (-15.0, 0.0), (0.0, 15.0), (0.0, -15.0)):
        bolt_scores.append(1.0 if len(_z_hits(mesh, x, y)) == 0 else 0.0)
    holes = 0.45 * center_bore_open + 0.55 * float(np.mean(bolt_scores))

    step_gap = 1.0 if len(_z_hits(mesh, 16.0, 0.0)) == 0 else 0.0
    task = 0.45 * holes + 0.35 * profile + 0.20 * step_gap
    composite = _conservative_composite(
        pose, dims, task, pose_weight=0.25, dims_weight=0.25, task_weight=0.50
    )
    return {
        "build_success": 1.0,
        "composite": _clamp01(composite),
        "pose_score": _clamp01(pose),
        "geometry_score": _clamp01((pose + dims) / 2.0),
        "task_score": _clamp01(task),
        "task_specific_score": _clamp01(task),
    }


def _eval_medium_ring(
    mesh: trimesh.Trimesh, expected: dict[str, float]
) -> dict[str, float]:
    bbox = _bbox_metrics(mesh)
    outer_d = _estimate_outer_d(mesh)
    inner_d = _estimate_bore_d(mesh, max_r=10.5)
    pose = (
        0.30 * _near_score(bbox["min_z"], expected["min_z_mm"], 0.05)
        + 0.30 * _near_score(bbox["max_z"], expected["max_z_mm"], 0.08)
        + 0.20 * _near_score(bbox["center_x"], expected["center_x_mm"], 0.10)
        + 0.20 * _near_score(bbox["center_y"], expected["center_y_mm"], 0.10)
    )
    dims = (
        0.38 * _near_score(outer_d, expected["outer_d_mm"], 0.25)
        + 0.38 * _near_score(inner_d, expected["inner_d_mm"], 0.20)
        + 0.24 * _near_score(bbox["size_z"], expected["height_mm"], 0.08)
    )
    center_open = 1.0 if len(_z_hits(mesh, 0.0, 0.0)) == 0 else 0.0
    ring_solid = 1.0 if len(_z_hits(mesh, 12.0, 0.0)) >= 2 else 0.0
    outside_empty = 1.0 if len(_z_hits(mesh, 16.0, 0.0)) == 0 else 0.0
    task = 0.45 * center_open + 0.35 * ring_solid + 0.20 * outside_empty
    composite = _conservative_composite(
        pose, dims, task, pose_weight=0.34, dims_weight=0.36, task_weight=0.30
    )
    return {
        "build_success": 1.0,
        "composite": _clamp01(composite),
        "pose_score": _clamp01(pose),
        "geometry_score": _clamp01((pose + dims) / 2.0),
        "task_score": _clamp01(task),
        "task_specific_score": _clamp01(task),
    }


def _eval_medium_plate_slot(
    mesh: trimesh.Trimesh, expected: dict[str, float]
) -> dict[str, float]:
    bbox = _bbox_metrics(mesh)
    pose = (
        0.30 * _near_score(bbox["min_z"], expected["min_z_mm"], 0.05)
        + 0.30 * _near_score(bbox["max_z"], expected["max_z_mm"], 0.08)
        + 0.20 * _near_score(bbox["center_x"], 0.0, 0.12)
        + 0.20 * _near_score(bbox["center_y"], 0.0, 0.12)
    )
    dims = (
        0.34 * _near_score(bbox["size_x"], expected["size_x_mm"], 0.20)
        + 0.34 * _near_score(bbox["size_y"], expected["size_y_mm"], 0.20)
        + 0.32 * _near_score(bbox["size_z"], expected["size_z_mm"], 0.10)
    )
    slot_open_pts = [(-15.0, 0.0), (0.0, 0.0), (15.0, 0.0)]
    slot_open = float(
        np.mean(
            [1.0 if len(_z_hits(mesh, x, y)) == 0 else 0.0 for x, y in slot_open_pts]
        )
    )
    side_solid_pts = [(0.0, 12.0), (0.0, -12.0)]
    side_solid = float(
        np.mean(
            [1.0 if len(_z_hits(mesh, x, y)) >= 2 else 0.0 for x, y in side_solid_pts]
        )
    )
    hole_pts = [(30.0, 15.0), (30.0, -15.0), (-30.0, 15.0), (-30.0, -15.0)]
    holes_open = float(
        np.mean([1.0 if len(_z_hits(mesh, x, y)) == 0 else 0.0 for x, y in hole_pts])
    )
    task = 0.40 * slot_open + 0.35 * holes_open + 0.25 * side_solid
    composite = _conservative_composite(
        pose, dims, task, pose_weight=0.30, dims_weight=0.30, task_weight=0.40
    )
    return {
        "build_success": 1.0,
        "composite": _clamp01(composite),
        "pose_score": _clamp01(pose),
        "geometry_score": _clamp01((pose + dims) / 2.0),
        "task_score": _clamp01(task),
        "task_specific_score": _clamp01(task),
    }


def _eval_hard_hex_nut(
    mesh: trimesh.Trimesh, expected: dict[str, float]
) -> dict[str, float]:
    bbox = _bbox_metrics(mesh)
    z_mid = float((bbox["min_z"] + bbox["max_z"]) / 2.0)
    n = 180
    angles = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
    radii = np.array(
        [_radial_hit_radius(mesh, float(a), z_mid) for a in angles], dtype=float
    )
    r_min = float(np.quantile(radii, 0.05))
    r_max = float(np.quantile(radii, 0.98))
    harmonic_k, harmonic_amp = _estimate_tooth_count(mesh, z_mid)
    pose = (
        0.35 * _near_score(bbox["min_z"], expected["min_z_mm"], 0.05)
        + 0.35 * _near_score(bbox["max_z"], expected["max_z_mm"], 0.08)
        + 0.15 * _near_score(bbox["center_x"], 0.0, 0.10)
        + 0.15 * _near_score(bbox["center_y"], 0.0, 0.10)
    )
    af = expected["af_mm"]
    corner_r = af / (3.0**0.5)
    bore_d = _estimate_bore_d(mesh, max_r=6.0)
    dims = (
        0.25 * _near_score(r_min * 2.0, af, 0.35)
        + 0.25 * _near_score(r_max, corner_r, 0.25)
        + 0.25 * _near_score(bore_d, expected["bore_d_mm"], 0.40)
        + 0.25 * _near_score(bbox["size_z"], expected["thickness_mm"], 0.08)
    )
    bore_open = 1.0 if len(_z_hits(mesh, 0.0, 0.0)) == 0 else 0.0
    web_solid = 1.0 if len(_z_hits(mesh, 7.0, 0.0)) >= 2 else 0.0
    harmonic = 0.6 * _near_score(harmonic_k, 6.0, 1.0) + 0.4 * _clamp01(
        (harmonic_amp - 0.16) / 0.18
    )
    task = 0.34 * bore_open + 0.22 * web_solid + 0.44 * harmonic
    composite = _conservative_composite(
        pose, dims, task, pose_weight=0.24, dims_weight=0.30, task_weight=0.46
    )
    return {
        "build_success": 1.0,
        "composite": _clamp01(composite),
        "pose_score": _clamp01(pose),
        "geometry_score": _clamp01((pose + dims) / 2.0),
        "task_score": _clamp01(task),
        "task_specific_score": _clamp01(task),
    }


def _eval_hard_ring_bolt(
    mesh: trimesh.Trimesh, expected: dict[str, float]
) -> dict[str, float]:
    bbox = _bbox_metrics(mesh)
    outer_d = _estimate_outer_d(mesh)
    inner_d = _estimate_bore_d(mesh, max_r=15.5)
    pose = (
        0.30 * _near_score(bbox["min_z"], expected["min_z_mm"], 0.05)
        + 0.30 * _near_score(bbox["max_z"], expected["max_z_mm"], 0.08)
        + 0.20 * _near_score(bbox["center_x"], 0.0, 0.10)
        + 0.20 * _near_score(bbox["center_y"], 0.0, 0.10)
    )
    dims = (
        0.36 * _near_score(outer_d, expected["outer_d_mm"], 0.35)
        + 0.34 * _near_score(inner_d, expected["inner_d_mm"], 0.70)
        + 0.30 * _near_score(bbox["size_z"], expected["height_mm"], 0.10)
    )

    center_open = 1.0 if len(_z_hits(mesh, 0.0, 0.0)) == 0 else 0.0
    ring_solid = 1.0 if len(_z_hits(mesh, 17.0, 0.0)) >= 2 else 0.0
    outside_empty = 1.0 if len(_z_hits(mesh, 26.0, 0.0)) == 0 else 0.0

    bolt_xy: list[tuple[float, float]] = []
    for i in range(6):
        a = np.deg2rad(float(60.0 * i))
        bolt_xy.append((20.0 * float(np.cos(a)), 20.0 * float(np.sin(a))))
    bolts_open = float(
        np.mean([1.0 if len(_z_hits(mesh, x, y)) == 0 else 0.0 for x, y in bolt_xy])
    )

    between_xy: list[tuple[float, float]] = []
    for i in range(6):
        a = np.deg2rad(float(30.0 + 60.0 * i))
        between_xy.append((20.0 * float(np.cos(a)), 20.0 * float(np.sin(a))))
    between_solid = float(
        np.mean([1.0 if len(_z_hits(mesh, x, y)) >= 2 else 0.0 for x, y in between_xy])
    )

    task = (
        0.20 * center_open
        + 0.20 * ring_solid
        + 0.10 * outside_empty
        + 0.30 * bolts_open
        + 0.20 * between_solid
    )
    composite = _conservative_composite(
        pose, dims, task, pose_weight=0.26, dims_weight=0.29, task_weight=0.45
    )
    return {
        "build_success": 1.0,
        "composite": _clamp01(composite),
        "pose_score": _clamp01(pose),
        "geometry_score": _clamp01((pose + dims) / 2.0),
        "task_score": _clamp01(task),
        "task_specific_score": _clamp01(task),
    }


def _eval_hard_step_block(
    mesh: trimesh.Trimesh, expected: dict[str, float]
) -> dict[str, float]:
    bbox = _bbox_metrics(mesh)
    pose = (
        0.30 * _near_score(bbox["min_z"], expected["min_z_mm"], 0.05)
        + 0.30 * _near_score(bbox["max_z"], expected["max_z_mm"], 0.10)
        + 0.20 * _near_score(bbox["center_x"], 0.0, 0.12)
        + 0.20 * _near_score(bbox["center_y"], 0.0, 0.12)
    )
    dims = (
        0.34 * _near_score(bbox["size_x"], expected["size_x_mm"], 0.25)
        + 0.33 * _near_score(bbox["size_y"], expected["size_y_mm"], 0.25)
        + 0.33 * _near_score(bbox["size_z"], expected["size_z_mm"], 0.12)
    )

    hole_pts = [(15.0, 10.0), (15.0, -10.0), (-15.0, 10.0), (-15.0, -10.0)]
    holes_open = float(
        np.mean([1.0 if len(_z_hits(mesh, x, y)) == 0 else 0.0 for x, y in hole_pts])
    )

    top_base_only = _z_hits(mesh, 25.0, 15.0)
    top_mid = _z_hits(mesh, 18.0, 12.0)
    top_full = _z_hits(mesh, 8.0, 8.0)
    level_scores = [
        _near_score(top_base_only[0], 10.0, 0.30) if top_base_only else 0.0,
        _near_score(top_mid[0], 20.0, 0.30) if top_mid else 0.0,
        _near_score(top_full[0], 30.0, 0.30) if top_full else 0.0,
    ]
    levels = float(np.mean(level_scores))
    outside_empty = 1.0 if len(_z_hits(mesh, 31.0, 0.0)) == 0 else 0.0
    task = 0.40 * holes_open + 0.45 * levels + 0.15 * outside_empty
    composite = _conservative_composite(
        pose, dims, task, pose_weight=0.24, dims_weight=0.26, task_weight=0.50
    )
    return {
        "build_success": 1.0,
        "composite": _clamp01(composite),
        "pose_score": _clamp01(pose),
        "geometry_score": _clamp01((pose + dims) / 2.0),
        "task_score": _clamp01(task),
        "task_specific_score": _clamp01(task),
    }


def _eval_hard_plate_slot8(
    mesh: trimesh.Trimesh, expected: dict[str, float]
) -> dict[str, float]:
    bbox = _bbox_metrics(mesh)
    pose = (
        0.30 * _near_score(bbox["min_z"], expected["min_z_mm"], 0.05)
        + 0.30 * _near_score(bbox["max_z"], expected["max_z_mm"], 0.08)
        + 0.20 * _near_score(bbox["center_x"], 0.0, 0.12)
        + 0.20 * _near_score(bbox["center_y"], 0.0, 0.12)
    )
    dims = (
        0.34 * _near_score(bbox["size_x"], expected["size_x_mm"], 0.25)
        + 0.33 * _near_score(bbox["size_y"], expected["size_y_mm"], 0.25)
        + 0.33 * _near_score(bbox["size_z"], expected["size_z_mm"], 0.12)
    )

    slot_open_pts = [(-20.0, 0.0), (0.0, 0.0), (20.0, 0.0)]
    slot_open = float(
        np.mean(
            [1.0 if len(_z_hits(mesh, x, y)) == 0 else 0.0 for x, y in slot_open_pts]
        )
    )
    side_solid_pts = [(0.0, 10.0), (0.0, -10.0)]
    side_solid = float(
        np.mean(
            [1.0 if len(_z_hits(mesh, x, y)) >= 2 else 0.0 for x, y in side_solid_pts]
        )
    )
    hole_pts = [
        (40.0, 20.0),
        (40.0, -20.0),
        (20.0, 20.0),
        (20.0, -20.0),
        (-20.0, 20.0),
        (-20.0, -20.0),
        (-40.0, 20.0),
        (-40.0, -20.0),
    ]
    holes_open = float(
        np.mean([1.0 if len(_z_hits(mesh, x, y)) == 0 else 0.0 for x, y in hole_pts])
    )
    corner_solid = float(
        np.mean(
            [
                1.0 if len(_z_hits(mesh, x, y)) >= 2 else 0.0
                for x, y in ((48.0, 28.0), (-48.0, 28.0), (48.0, -28.0), (-48.0, -28.0))
            ]
        )
    )
    task = (
        0.30 * slot_open + 0.35 * holes_open + 0.20 * side_solid + 0.15 * corner_solid
    )
    composite = _conservative_composite(
        pose, dims, task, pose_weight=0.24, dims_weight=0.26, task_weight=0.50
    )
    return {
        "build_success": 1.0,
        "composite": _clamp01(composite),
        "pose_score": _clamp01(pose),
        "geometry_score": _clamp01((pose + dims) / 2.0),
        "task_score": _clamp01(task),
        "task_specific_score": _clamp01(task),
    }


select_gearbox_components = _select_gearbox_components
select_compound_gearbox_components = _select_compound_gearbox_components
select_right_angle_gearbox_solids = _select_right_angle_gearbox_solids
gear_layer_metrics = _gear_layer_metrics
gearbox_phase_trials = _gearbox_phase_trials
gearbox_sim_transfer_score = _gearbox_sim_transfer_score
export_mesh_stl = _export_mesh_stl
component_axis_from_bbox = _component_axis_from_bbox
component_attached_to_axis_line = _component_attached_to_axis_line
combine_components = _combine_components
solid_bbox_metrics = _solid_bbox_metrics
