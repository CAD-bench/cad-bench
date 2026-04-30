from __future__ import annotations

from dataclasses import dataclass
from http.cookiejar import CookieJar
import json
from pathlib import Path
import traceback
from typing import Any
from urllib.request import HTTPCookieProcessor, Request, build_opener

import tomllib

from bench.tasks_repo import task_manifest, tasks_root

ALLOWED_TASK_DIFFICULTIES = {"easy", "medium", "hard", "insane"}
REQUIRED_TASK_FIELDS = ("task_id", "difficulty", "order", "expected")
_MCMASTER_BASE_URL = "https://www.mcmaster.com"
_MCMASTER_ASSET_VERSION = "mv1771443571"


def task_dir(task_id: str) -> Path:
    return (tasks_root() / task_id).resolve()


def _validate_task_meta(task_id: str, data: dict[str, object]) -> dict[str, object]:
    missing = [field for field in REQUIRED_TASK_FIELDS if field not in data]
    if missing:
        raise KeyError(f"task {task_id} is missing required fields: {missing}")
    if str(data["task_id"]) != task_id:
        raise ValueError(f"task {task_id} has mismatched task_id={data['task_id']!r}")
    if str(data["difficulty"]) not in ALLOWED_TASK_DIFFICULTIES:
        raise ValueError(
            f"task {task_id} has unsupported difficulty={data['difficulty']!r}"
        )
    order = int(data["order"])
    if order <= 0:
        raise ValueError(f"task {task_id} must have positive order")
    expected = data["expected"]
    if not isinstance(expected, dict) or not expected:
        raise TypeError(f"task {task_id} must define a non-empty [expected] table")
    if "reference_step_fixture" in data:
        fixture_path = task_dir(task_id) / str(data["reference_step_fixture"])
        if not fixture_path.exists():
            raise FileNotFoundError(
                f"task {task_id} references missing fixture: {fixture_path}"
            )
    if "module_weights" in data:
        weights = data["module_weights"]
        if not isinstance(weights, dict) or not weights:
            raise TypeError(
                f"task {task_id} must define a non-empty [module_weights] table"
            )
        if sum(float(value) for value in dict(weights).values()) <= 0.0:
            raise ValueError(f"task {task_id} must have positive total module weight")
    return data


def task_meta(task_id: str) -> dict[str, object]:
    data = tomllib.loads(
        task_dir(task_id).joinpath("task.toml").read_text(encoding="utf-8")
    )
    return _validate_task_meta(task_id, data)


def task_text(task_id: str, filename: str) -> str:
    text = task_dir(task_id).joinpath(filename).read_text(encoding="utf-8").strip()
    return text.replace("__TASK_DIR__", task_dir(task_id).as_posix())


@dataclass(frozen=True)
class BenchmarkDefinition:
    name: str
    mode: str
    system_prompt: str | None
    parser: Any
    examples: tuple[dict[str, Any], ...]
    submission_step_path: str | None = None


@dataclass(frozen=True)
class TaskEvaluation:
    raw: dict[str, Any]
    metrics: dict[str, Any]


def _failed_task_evaluation(stage: str, exc: Exception) -> TaskEvaluation:
    raw = {
        "build_success": 0.0,
        "reward": 0.0,
        "task_score": 0.0,
        "overall_score": 0.0,
        "failure_stage": stage,
        "error_type": type(exc).__name__,
        "error_message": str(exc),
    }
    return TaskEvaluation(raw=raw, metrics=dict(raw))


def _write_scoring_failure_provenance(
    provenance_dir: Path | None,
    stage: str,
    exc: Exception,
) -> None:
    if provenance_dir is None:
        return
    provenance_dir.mkdir(parents=True, exist_ok=True)
    failure_path = provenance_dir / "scoring_failure.json"
    traceback_path = provenance_dir / "scoring_failure_traceback.txt"
    failure_path.write_text(
        json.dumps(
            {
                "failure_stage": stage,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    traceback_path.write_text(traceback.format_exc(), encoding="utf-8")


@dataclass(frozen=True)
class TaskModuleSpec:
    task_id: str
    difficulty: str
    order: int
    expected: dict[str, object]
    prompt: str
    reference_solution_code: str
    evaluator_name: str = ""
    part_aware: bool = False
    reference_geometry_gate: bool = False
    render_reference_video_name: str = ""
    task_dir: Path | None = None

    def evaluate_code(
        self,
        code: str,
        *,
        llm_generated: bool = True,
        provenance_dir: Path | None = None,
    ) -> TaskEvaluation:
        if not code.strip():
            raw = {
                "build_success": 0.0,
                "reward": 0.0,
                "task_score": 0.0,
                "overall_score": 0.0,
                "failure_stage": "invalid_submission",
                "error_type": "ValueError",
                "error_message": "candidate code is empty",
            }
            return TaskEvaluation(raw=raw, metrics=dict(raw))

        from contextlib import ExitStack

        from tasks import utils as task_utils

        evaluator = getattr(task_utils, self.evaluator_name)
        with ExitStack() as stack:
            if provenance_dir is not None:
                stack.enter_context(task_utils.scoring_provenance_dir(provenance_dir))
            if llm_generated:
                stack.enter_context(task_utils.llm_generated_code_context())
            try:
                part = task_utils.execute_submission_code(code)
            except Exception as exc:
                _write_scoring_failure_provenance(
                    provenance_dir, "execution_error", exc
                )
                return _failed_task_evaluation("execution_error", exc)
            try:
                mesh = task_utils.mesh_from_part(part)
            except Exception as exc:
                _write_scoring_failure_provenance(provenance_dir, "mesh_error", exc)
                return _failed_task_evaluation("mesh_error", exc)
            try:
                raw = (
                    evaluator(part, mesh, self.expected)
                    if self.part_aware
                    else evaluator(mesh, self.expected)
                )
                if self.reference_geometry_gate:
                    raw = task_utils._apply_reference_geometry_gate(
                        self.task_id, part, raw
                    )
            except Exception as exc:
                _write_scoring_failure_provenance(
                    provenance_dir, "evaluation_error", exc
                )
                return _failed_task_evaluation("evaluation_error", exc)
        normalized = task_utils.normalize_score_result(raw)
        normalized["failure_stage"] = str(normalized.get("failure_stage", ""))
        normalized["error_type"] = str(normalized.get("error_type", ""))
        normalized["error_message"] = str(normalized.get("error_message", ""))
        normalized.pop("composite_raw", None)
        return TaskEvaluation(raw=dict(raw), metrics=normalized)

    def evaluate_step(
        self, step_path: Path, *, provenance_dir: Path | None = None
    ) -> TaskEvaluation:
        if not step_path.exists() or step_path.stat().st_size <= 0:
            raw = {
                "build_success": 0.0,
                "reward": 0.0,
                "task_score": 0.0,
                "overall_score": 0.0,
                "failure_stage": "invalid_submission",
                "error_type": "ValueError",
                "error_message": f"STEP submission is missing or empty: {step_path}",
            }
            return TaskEvaluation(raw=raw, metrics=dict(raw))
        code = f"from build123d import *\npart = import_step({step_path.as_posix()!r})\n"
        return self.evaluate_code(
            code,
            llm_generated=False,
            provenance_dir=provenance_dir,
        )

    def __call__(
        self, step_path: Path, *, provenance_dir: Path | None = None
    ) -> TaskEvaluation:
        return self.evaluate_step(step_path, provenance_dir=provenance_dir)

    @property
    def has_functional_video(self) -> bool:
        return bool(self.render_reference_video_name)

    def render_reference_video(self, out_dir: Path) -> tuple[Path, Path]:
        if not self.render_reference_video_name:
            raise ValueError(f"unsupported functional video task: {self.task_id}")
        from tasks import rendering as task_rendering

        renderer = getattr(task_rendering, self.render_reference_video_name)
        return renderer(self, out_dir)


def load_task_spec(task_id: str) -> TaskModuleSpec:
    meta = task_meta(task_id)
    expected = dict(meta["expected"])
    return TaskModuleSpec(
        task_id=str(meta["task_id"]),
        difficulty=str(meta["difficulty"]),
        order=int(meta["order"]),
        expected=expected,
        prompt=task_text(task_id, "prompt.txt"),
        reference_solution_code=task_text(task_id, "gold.py"),
        evaluator_name=str(meta["evaluator"]),
        part_aware=bool(meta.get("part_aware", False)),
        reference_geometry_gate=bool(meta.get("reference_geometry_gate", False)),
        render_reference_video_name=str(meta.get("render_reference_video", "")).strip(),
        task_dir=task_dir(task_id),
    )


def load_all_task_specs() -> dict[str, TaskModuleSpec]:
    specs = sorted(
        (
            load_task_spec(str(item["task_id"]))
            for item in task_manifest()
            if str(item.get("task_id", "")).strip()
        ),
        key=lambda spec: (int(spec.order), str(spec.task_id)),
    )
    return {spec.task_id: spec for spec in specs}


def _mcmaster_bootstrap_session(part_number: str, user_agent: str) -> tuple[Any, str]:
    cookie_jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))
    opener.open(
        Request(
            f"{_MCMASTER_BASE_URL}/{part_number}/", headers={"User-Agent": user_agent}
        )
    )
    asset_version = _MCMASTER_ASSET_VERSION
    for cookie in cookie_jar:
        if cookie.name == "volver" and cookie.value:
            asset_version = cookie.value
            break
    opener.open(
        Request(
            f"{_MCMASTER_BASE_URL}/{asset_version}/tokenauthorization.aspx",
            data=b"",
            headers={
                "User-Agent": user_agent,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        ),
    )
    opener.open(
        Request(
            f"{_MCMASTER_BASE_URL}/{asset_version}/Vldt.aspx",
            headers={"User-Agent": user_agent},
        )
    )
    return opener, asset_version


def fetch_mcmaster_step(
    part_number: str, out_path: Path, user_agent: str = "cad-build123d-bench/0.1"
) -> Path:
    opener, asset_version = _mcmaster_bootstrap_session(part_number, user_agent)
    step_url = f"{_MCMASTER_BASE_URL}/{asset_version}/download_3d_cad.aspx?partnbr={part_number}&format=3d-step&ctlg=mc-master"
    req = Request(
        step_url,
        headers={
            "User-Agent": user_agent,
            "Referer": f"{_MCMASTER_BASE_URL}/{part_number}/",
        },
    )
    with opener.open(req) as resp:
        data = resp.read()
    if len(data) < 128 or not data.startswith((b"ISO-10303-21", b"PK\\x03\\x04")):
        raise RuntimeError(f"McMaster STEP download looked invalid for {part_number}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    return out_path


def ensure_mcmaster_step_fixture(
    task_id: str, relative_path: str, part_number: str
) -> Path:
    fixture_path = task_dir(task_id) / relative_path
    if fixture_path.exists():
        return fixture_path
    return fetch_mcmaster_step(part_number, fixture_path)
