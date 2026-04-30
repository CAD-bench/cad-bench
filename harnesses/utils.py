from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

XML_CODE_SYSTEM_PROMPT = "You are a CAD coding model. Return only XML with Build123D code. No prose, no markdown fences."
SUBMISSION_STEP_PATH = "/submission/final.step"
CONTAINER_APP_DIR = "/app"
CONTAINER_CODEX_DIR = "/root/.codex"
CONTAINER_WORKDIR = "/workspace"
CONTAINER_SUBMISSION_DIR = "/submission"
CONTAINER_HOME_STEP_PATH = "/workspace/final.step"
CONTAINER_DOCS_DIR = "/task/build123d_docs"
CONTAINER_OFFLINE_SITE_PACKAGES_DIR = "/opt/cad-bench-site-packages"
CONTAINER_PROJECT_SITE_PACKAGES_DIR = "/app/.venv/lib/python3.11/site-packages"
CONTAINER_PI_DIR = "/root/.pi"
PI_WEB_EXTENSION_CONTAINER_PATH = "/usr/local/lib/node_modules/pi-web-access/index.ts"
_SENSITIVE_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD")

SUPPORTED_PROMPT_ACCESS_MODES = ("web", "offline", "offline_nodocs", "web_ci")
BUILTIN_HARNESS_PROVIDERS = (
    "pi",
    "codex",
    "openai",
    "openai_codex",
    "kimi",
    "deepseek",
    "openrouter",
    "vercel",
    "gemini",
    "claude",
)
REASONING_EFFORTS = {"low": "low", "med": "medium", "high": "high", "xhigh": "xhigh"}

HARNESSES_ROOT = Path(__file__).resolve().parent
REPO_ROOT = HARNESSES_ROOT.parent
DOCS_SOURCE_DIR = HARNESSES_ROOT / "build123d_docs"
DOCS_CACHE_DIR = REPO_ROOT / ".cache" / "build123d_docs_full"
RUNTIME_HOME_TMP_ROOT = REPO_ROOT / ".tmp" / "runtime_homes"
OFFLINE_SITE_PACKAGES_DIR = (
    REPO_ROOT
    / ".venv"
    / "lib"
    / f"python{sys.version_info.major}.{sys.version_info.minor}"
    / "site-packages"
)
PROMPTS_DIR = HARNESSES_ROOT / "prompts"
DOCS_TOP_ONLY_MODULES = (
    "00_start_here.md",
    "01_cheat_sheet.md",
    "02_key_concepts_builder.md",
    "03_key_concepts_algebra.md",
    "04_objects.md",
    "05_operations.md",
    "06_topology_selection.md",
    "07_builder_api_reference.md",
    "08_direct_api_reference.md",
)


class CADCodeParser:
    _pattern = re.compile(
        r"<build123d_code>\s*(.*?)\s*</build123d_code>", flags=re.DOTALL
    )

    def parse_answer(self, completion: Any) -> str | None:
        content = _completion_to_text(completion)
        match = self._pattern.search(content)
        return match.group(1).strip() if match else None


def _completion_to_text(completion: Any) -> str:
    if isinstance(completion, str):
        return completion
    if isinstance(completion, dict):
        content = completion.get("content")
        if isinstance(content, str):
            return content
        raise TypeError("completion dict must contain string content")
    if isinstance(completion, list):
        for message in reversed(completion):
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]
        raise TypeError(
            "completion list must contain at least one message with string content"
        )
    raise TypeError(f"unsupported completion type: {type(completion).__name__}")


def strip_xml_return_block(prompt: str) -> str:
    marker = "Return XML only:"
    return prompt.split(marker, 1)[0].strip() if marker in prompt else prompt.strip()


@lru_cache(maxsize=None)
def load_prompt_template(name: str) -> str:
    path = PROMPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"missing prompt template: {path}")
    return path.read_text(encoding="utf-8").strip()


def render_prompt_template(name: str, **values: str) -> str:
    return load_prompt_template(name).format(**values).strip()


def generate_build123d_docs_bundle(
    output_dir: Path, include_submodules: bool = True
) -> Path:
    module_paths = sorted((DOCS_SOURCE_DIR / "modules").glob("*.md"))
    if not include_submodules:
        keep = set(DOCS_TOP_ONLY_MODULES)
        module_paths = [path for path in module_paths if path.name in keep]
    if not module_paths:
        raise FileNotFoundError(
            f"checked-in docs bundle is missing under {DOCS_SOURCE_DIR}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    modules_dir = output_dir / "modules"
    if modules_dir.exists():
        shutil.rmtree(modules_dir)
    modules_dir.mkdir(parents=True, exist_ok=True)
    index_lines = [
        "# Build123D Local API Docs",
        "",
        "Checked-in curated markdown bundle used by offline harnesses.",
        "",
        "## Pages",
    ]
    single_turn_chunks: list[str] = []
    for path in module_paths:
        shutil.copy2(path, modules_dir / path.name)
        index_lines.append(f"- `{path.stem}` -> `modules/{path.name}`")
        single_turn_chunks.append(
            f"## modules/{path.name}\n\n{path.read_text(encoding='utf-8').strip()}"
        )
    (output_dir / "index.md").write_text(
        "\n".join(index_lines) + "\n", encoding="utf-8"
    )
    (output_dir / "single_turn.md").write_text(
        "\n\n".join(single_turn_chunks) + "\n", encoding="utf-8"
    )
    return output_dir


@lru_cache(maxsize=1)
def ensure_build123d_docs_bundle(cache_dir: str = str(DOCS_CACHE_DIR)) -> Path:
    out_dir = Path(cache_dir)
    modules_dir = out_dir / "modules"
    if (
        (out_dir / "single_turn.md").exists()
        and modules_dir.exists()
        and any(modules_dir.glob("*.md"))
    ):
        return out_dir
    generate_build123d_docs_bundle(out_dir, include_submodules=True)
    return out_dir


def single_turn_docs_text(docs_dir: Path) -> str:
    text = (docs_dir / "single_turn.md").read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"docs bundle is missing single_turn.md content: {docs_dir}")
    return text


def compose_single_turn_prompt(
    task_prompt: str,
    *,
    access_mode: str = "web",
    docs_text: str = "",
) -> str:
    if access_mode not in SUPPORTED_PROMPT_ACCESS_MODES:
        raise ValueError(
            f"Unsupported access mode: {access_mode}. Use one of {SUPPORTED_PROMPT_ACCESS_MODES}"
        )
    if access_mode == "offline" and not docs_text.strip():
        raise ValueError("docs_text is required for offline single-turn prompts")
    template_name = {
        "web": "one_shot_web.txt",
        "offline": "one_shot_offline.txt",
        "offline_nodocs": "one_shot_offline_nodocs.txt",
        "web_ci": "one_shot_web_ci.txt",
    }[access_mode]
    return render_prompt_template(
        template_name, task_prompt=task_prompt.strip(), docs_text=docs_text.strip()
    )


def build_single_turn_prompt(
    spec: Any, task_prompt: str, docs_dir: Path | None = None
) -> tuple[str | None, str]:
    docs_text = ""
    if str(spec.access) == "offline":
        docs_root = docs_dir or ensure_build123d_docs_bundle()
        docs_text = single_turn_docs_text(docs_root)
    return getattr(spec, "system_prompt", None), compose_single_turn_prompt(
        task_prompt,
        access_mode=str(spec.access),
        docs_text=docs_text,
    )


def compose_step_submission_prompt(task_prompt: str, access_mode: str) -> str:
    if access_mode not in SUPPORTED_PROMPT_ACCESS_MODES:
        raise ValueError(
            f"Unsupported access mode: {access_mode}. Use one of {SUPPORTED_PROMPT_ACCESS_MODES}"
        )
    template_name = (
        "agent_step_offline.txt" if access_mode == "offline" else "agent_step_web.txt"
    )
    return render_prompt_template(
        template_name, task_prompt=strip_xml_return_block(task_prompt)
    )


def build_step_prompt(spec: Any, task_prompt: str) -> tuple[str | None, str]:
    return (
        None,
        compose_step_submission_prompt(
            task_prompt=task_prompt,
            access_mode=str(spec.access),
        ),
    )


def offline_agent_site_packages_dir() -> Path:
    site_packages_dir = OFFLINE_SITE_PACKAGES_DIR.resolve()
    build123d_init = site_packages_dir / "build123d" / "__init__.py"
    if not build123d_init.exists():
        raise FileNotFoundError(
            f"offline agent python packages are missing build123d at {build123d_init}; "
            "install project dependencies into .venv before running offline harnesses"
        )
    return site_packages_dir


def offline_agent_container_env() -> dict[str, str]:
    return {"PYTHONPATH": CONTAINER_OFFLINE_SITE_PACKAGES_DIR}


def web_agent_container_env() -> dict[str, str]:
    return {"PYTHONPATH": CONTAINER_PROJECT_SITE_PACKAGES_DIR}


@dataclass(frozen=True)
class AgentRunResult:
    response: str
    error: str | None
    stdout: str
    stderr: str
    returncode: int | None
    command: list[str]
    session_id: str
    usage: dict[str, Any]
    parsed_events: list[dict[str, Any]]
    artifact_contents: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class HarnessSpec:
    harness_id: str
    provider: str
    strategy: str
    model: str
    reasoning_effort: str = ""
    access: str = "web"
    system_prompt: str | None = None
    file_path: str = ""
    symbol_name: str = ""


def harness(
    harness_id: str,
    *,
    provider: str,
    strategy: str,
    model: str,
    reasoning_effort: str = "",
    access: str = "web",
    system_prompt: str | None = None,
) -> HarnessSpec:
    return HarnessSpec(
        harness_id=harness_id,
        provider=provider,
        strategy=strategy,
        model=model,
        reasoning_effort=reasoning_effort,
        access=access,
        system_prompt=system_prompt,
    )


def needs_offline_docs(spec: Any) -> bool:
    return str(spec.access) == "offline"


def parse_json_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        value = json.loads(stripped)
        if isinstance(value, dict):
            events.append(value)
    return events


def _looks_sensitive_env_name(name: str) -> bool:
    upper = str(name or "").strip().upper()
    return bool(upper) and any(marker in upper for marker in _SENSITIVE_ENV_MARKERS)


def sanitize_env_var_spec(env_spec: str) -> str:
    name, sep, _value = str(env_spec).partition("=")
    if sep and _looks_sensitive_env_name(name):
        return f"{name}=REDACTED"
    return str(env_spec)


def sanitize_command_for_logging(command: list[str]) -> list[str]:
    sanitized: list[str] = []
    index = 0
    while index < len(command):
        part = str(command[index])
        if part in {"-e", "--env"} and index + 1 < len(command):
            sanitized.extend([part, sanitize_env_var_spec(str(command[index + 1]))])
            index += 2
            continue
        sanitized.append(part)
        index += 1
    return sanitized


def model_to_slug(model: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", str(model)).strip("_")
    if not slug:
        raise ValueError(f"unsupported built-in harness model: {model}")
    return slug


def builtin_harness_name(model: str, access: str, level: str) -> str:
    return f"{model_to_slug(model)}_{access}_{level}"


def builtin_harness_ref(provider: str, model: str, access: str, level: str) -> str:
    return f"{(HARNESSES_ROOT / provider / 'harnesses.py').resolve()}:{builtin_harness_name(model, access, level)}"


def register_builtin_harnesses(
    namespace: dict[str, Any],
    *,
    configs: tuple[tuple[str, str, str], ...],
    make_harness: Callable[[str, str, str], Any],
) -> tuple[tuple[str, str, str], ...]:
    for model, access, level in configs:
        name = builtin_harness_name(model, access, level)

        def factory(
            model_name: str = model,
            access_mode: str = access,
            level_name: str = level,
        ):
            return make_harness(model_name, access_mode, level_name)

        factory.__name__ = name
        namespace[name] = factory
    return configs


def _load_module(path: Path, kind: str) -> Any:
    module_name = "_cad_bench_dynamic_" + re.sub(
        r"[^a-zA-Z0-9_]+", "_", path.as_posix()
    )
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"failed to load {kind} module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def provider_module(provider: str) -> Any:
    path = HARNESSES_ROOT / provider / "harnesses.py"
    return _load_module(path.resolve(), "harness")


@lru_cache(maxsize=1)
def builtin_harness_matrix() -> tuple[tuple[str, str, str, str], ...]:
    matrix: list[tuple[str, str, str, str]] = []
    for provider in BUILTIN_HARNESS_PROVIDERS:
        module = provider_module(provider)
        configs = getattr(module, "SUPPORTED_CONFIGS", ())
        for model, access, level in configs:
            matrix.append((provider, str(model), str(access), str(level)))
    return tuple(matrix)


def split_harness_ref(value: str | Path) -> tuple[Path, str]:
    if isinstance(value, Path):
        raw = value
        symbol_name = ""
    else:
        raw_text = value.strip()
        if ".py:" not in raw_text:
            raise ValueError(
                "harness refs must use the form path/to/harnesses.py:symbol_name"
            )
        path_text, symbol_name = raw_text.rsplit(":", 1)
        raw = Path(path_text)
    return raw, symbol_name.strip()


def format_harness_ref(path: Path, symbol_name: str) -> str:
    return f"{path.resolve()}:{symbol_name}"


def resolve_harness_spec_path(value: str | Path) -> Path:
    raw, _ = split_harness_ref(value)
    candidate = raw if raw.suffix == ".py" else raw.with_suffix(".py")
    if candidate.exists():
        return candidate.resolve()
    raise FileNotFoundError(f"Harness spec path does not exist: {value}")


def load_harness_spec(value: str | Path) -> HarnessSpec:
    raw_path, symbol_name = split_harness_ref(value)
    if not symbol_name:
        raise ValueError(
            "harness refs must name an exported built-in function inside harnesses.py"
        )
    resolved = resolve_harness_spec_path(raw_path)
    module = _load_module(resolved, "harness")
    value = getattr(module, symbol_name, None)
    if value is None:
        raise AttributeError(f"harness module {resolved} must define `{symbol_name}`")
    raw_spec = value() if callable(value) else value
    spec = HarnessSpec(
        harness_id=str(raw_spec.harness_id),
        provider=str(raw_spec.provider),
        strategy=str(raw_spec.strategy),
        model=str(raw_spec.model),
        reasoning_effort=str(raw_spec.reasoning_effort),
        access=str(raw_spec.access),
        system_prompt=None
        if raw_spec.system_prompt is None
        else str(raw_spec.system_prompt),
        file_path=str(resolved),
        symbol_name=symbol_name,
    )
    return spec


def harness_module(spec: HarnessSpec) -> Any:
    if not spec.file_path:
        raise ValueError("Harness spec is missing file_path")
    return _load_module(Path(spec.file_path), "harness")
