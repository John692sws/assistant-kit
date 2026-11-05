"""Task registry: loads tasks/<action>/{task.yaml, prompt.md, output.schema.json}."""

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from ultron_hub.common.errors import TaskConfigError, TaskNotFoundError, TemplateError
from ultron_hub.core.collectors import COLLECTORS
from ultron_hub.core.job import validate_action

VALID_MODELS = ("haiku", "sonnet", "opus")
_TASK_KEYS = {"name", "description", "model", "timeout_s", "tools", "allowed_tools", "add_dirs", "collect", "max_budget_usd"}

# Paths a task must never be able to reach through --add-dir.
_HOME = Path.home()
FORBIDDEN_PATHS = (
    _HOME / ".ssh",
    _HOME / ".sudo_pass",
    _HOME / ".hermes",
    _HOME / ".claude",  # holds the CLI credentials
    Path("/etc"),
    Path("/root"),
)

# `{{` and `}}` are literal braces; `{name}` is a placeholder; any other brace is left untouched.
_PLACEHOLDER_RE = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render_template(template: str, params: dict[str, Any]) -> str:
    """Single-pass substitution: values are inserted verbatim, never re-expanded."""
    missing = sorted(
        {m.group(1) for m in _PLACEHOLDER_RE.finditer(template) if m.group(1) and m.group(1) not in params}
    )
    if missing:
        raise TemplateError(f"missing params for prompt: {', '.join(missing)}")

    def substitute(match: re.Match) -> str:
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        value = params[match.group(1)]
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    return _PLACEHOLDER_RE.sub(substitute, template)


@dataclass(frozen=True)
class TaskSpec:
    name: str
    description: str
    model: str
    timeout_s: float
    tools: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    add_dirs: tuple[str, ...]
    prompt_template: str
    output_schema: dict | None
    path: Path
    collect: str | None = None  # name in core.collectors.COLLECTORS; its data fills {metrics}
    max_budget_usd: float | None = None  # passed as --max-budget-usd
    settings: dict | None = None  # passed as --settings (e.g. the OS sandbox of mission profiles)

    def render_prompt(self, params: dict[str, Any]) -> str:
        return render_template(self.prompt_template, params)


def _is_relative_to(a: Path, b: Path) -> bool:
    return a == b or b in a.parents


def _check_add_dir(raw: Any, task: str) -> str:
    if not isinstance(raw, str) or not raw.startswith("/"):
        raise TaskConfigError(f"{task}: add_dirs entries must be absolute paths, got {raw!r}")
    path = Path(raw).resolve()
    for forbidden in FORBIDDEN_PATHS:
        # Refuse the forbidden path itself, anything inside it, and any parent of it (e.g. ~ or /).
        if _is_relative_to(path, forbidden) or _is_relative_to(forbidden, path):
            raise TaskConfigError(f"{task}: add_dir {raw} would expose {forbidden}")
    return str(path)


def scope_reads(spec: TaskSpec, directory: Path | str) -> TaskSpec:
    """The spec limited to reading inside one absolute folder known only at run time (the images of one video).

    `--add-dir` makes the folder reachable and a scoped `Read(//dir/**)` rule is the only permission: a task whose
    task.yaml lists `Read` without an `allowed_tools` rule can read nothing until a caller scopes it.
    """
    folder = _check_add_dir(str(directory), spec.name)
    if "Read" not in spec.tools:
        raise TaskConfigError(f"{spec.name}: cannot scope reads, `Read` is not in tools")
    return replace(spec, add_dirs=(*spec.add_dirs, folder),
                   allowed_tools=(*spec.allowed_tools, f"Read(//{folder.strip('/')}/**)"))


def _str_list(value: Any, key: str, task: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise TaskConfigError(f"{task}: `{key}` must be a list of non-empty strings")
    return tuple(value)


class Registry:
    def __init__(self, tasks_dir: Path, default_model: str = "sonnet", default_timeout_s: float = 300):
        if default_model not in VALID_MODELS:
            raise TaskConfigError(f"invalid default model: {default_model!r}")
        self.tasks_dir = Path(tasks_dir)
        self.default_model = default_model
        self.default_timeout_s = default_timeout_s

    def list_actions(self) -> list[str]:
        if not self.tasks_dir.is_dir():
            return []
        return sorted(p.name for p in self.tasks_dir.iterdir() if (p / "task.yaml").is_file())

    def load(self, action: str) -> TaskSpec:
        validate_action(action)
        task_dir = self.tasks_dir / action
        task_file = task_dir / "task.yaml"
        if not task_file.is_file():
            raise TaskNotFoundError(f"unknown action: {action}")

        try:
            raw = yaml.safe_load(task_file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise TaskConfigError(f"{action}: invalid task.yaml: {exc}") from exc
        if not isinstance(raw, dict):
            raise TaskConfigError(f"{action}: task.yaml must be a mapping")
        unknown = set(raw) - _TASK_KEYS
        if unknown:
            raise TaskConfigError(f"{action}: unknown keys in task.yaml: {sorted(unknown)}")
        if raw.get("name", action) != action:
            raise TaskConfigError(f"{action}: task.yaml name {raw['name']!r} != directory name")

        model = raw.get("model") or self.default_model
        if model not in VALID_MODELS:
            raise TaskConfigError(f"{action}: invalid model {model!r} (expected one of {VALID_MODELS})")

        timeout = raw.get("timeout_s", self.default_timeout_s)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise TaskConfigError(f"{action}: timeout_s must be a positive number")

        prompt_file = task_dir / "prompt.md"
        if not prompt_file.is_file():
            raise TaskConfigError(f"{action}: missing prompt.md")
        prompt = prompt_file.read_text(encoding="utf-8")
        if not prompt.strip():
            raise TaskConfigError(f"{action}: prompt.md is empty")

        schema = None
        schema_file = task_dir / "output.schema.json"
        if schema_file.is_file():
            try:
                schema = json.loads(schema_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise TaskConfigError(f"{action}: invalid output.schema.json: {exc}") from exc
            if not isinstance(schema, dict):
                raise TaskConfigError(f"{action}: output.schema.json must be an object")

        collect = raw.get("collect")
        if collect is not None and collect not in COLLECTORS:
            raise TaskConfigError(f"{action}: unknown collector {collect!r} (expected one of {sorted(COLLECTORS)})")

        budget = raw.get("max_budget_usd")
        if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0):
            raise TaskConfigError(f"{action}: max_budget_usd must be a positive number")

        return TaskSpec(
            name=action,
            description=str(raw.get("description") or ""),
            model=model,
            timeout_s=float(timeout),
            tools=_str_list(raw.get("tools"), "tools", action),
            allowed_tools=_str_list(raw.get("allowed_tools"), "allowed_tools", action),
            add_dirs=tuple(_check_add_dir(d, action) for d in _str_list(raw.get("add_dirs"), "add_dirs", action)),
            prompt_template=prompt,
            output_schema=schema,
            path=task_dir,
            collect=collect,
            max_budget_usd=float(budget) if budget is not None else None,
        )
