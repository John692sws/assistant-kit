"""Orchestration: request -> task spec -> executor -> persisted result."""

import json
import time
from pathlib import Path
from typing import Any

from ultron_hub.common.errors import UltronHubError
from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import Settings
from ultron_hub.core.collectors import COLLECTORS, PARAM_COLLECTORS
from ultron_hub.core.job import JobRequest, JobResult
from ultron_hub.core.registry import Registry, scope_reads
from ultron_hub.executors.claude_cli import ClaudeCliExecutor

log = get_logger("runner")


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def run_job(request: JobRequest, settings: Settings, executor: ClaudeCliExecutor | None = None,
            read_dir: Path | None = None) -> JobResult:
    """`read_dir`: the one folder the task may read (see registry.scope_reads), decided by the caller, not by Claude."""
    start = time.monotonic()
    run_dir = settings.runs_dir / request.job_id
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return JobResult(job_id=request.job_id, action=request.action, status="error",
                         summary="duplicate job_id", error=f"run directory already exists: {run_dir}")

    _write_json(run_dir / "request.json", request.to_dict())
    log.info("job=%s action=%s requested_by=%s received", request.job_id, request.action, request.requested_by)

    model = ""
    try:
        registry = Registry(settings.tasks_dir, settings.default_model, settings.default_timeout_s)
        spec = registry.load(request.action)
        model = spec.model
        if read_dir is not None:
            spec = scope_reads(spec, read_dir)
        params = request.params
        if spec.collect:
            collector = COLLECTORS[spec.collect]
            metrics = collector(params) if spec.collect in PARAM_COLLECTORS else collector()
            _write_json(run_dir / "collected.json", metrics)
            params = {**params, "metrics": metrics}  # collected data wins over a caller-supplied value
        prompt = spec.render_prompt(params)
        executor = executor or ClaudeCliExecutor(settings.claude_binary, settings.disallowed_tools,
                                           settings.claude_retry_delay_s)
        result = executor.run(request, spec, prompt, run_dir)
    except UltronHubError as exc:
        log.warning("job=%s rejected: %s", request.job_id, exc)
        result = JobResult(job_id=request.job_id, action=request.action, status="error",
                           summary="job rejected", error=str(exc), model=model,
                           duration_s=time.monotonic() - start)
    except Exception as exc:  # never leave a run without result.json
        log.exception("job=%s internal error", request.job_id)
        result = JobResult(job_id=request.job_id, action=request.action, status="error",
                           summary="internal error", error=f"{type(exc).__name__}: {exc}", model=model,
                           duration_s=time.monotonic() - start)

    _write_json(run_dir / "result.json", result.to_dict())
    return result
