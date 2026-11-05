"""Executor running a task through `claude -p` (headless Claude Code).

Rules learned on this machine:
- never `--bare` (it ignores the stored login -> "Not logged in");
- never `--dangerously-skip-permissions` / `bypassPermissions`;
- always pass `--model` explicitly;
- success = exit code 0 AND `is_error is False` (never trust `subtype`);
- the prompt goes through stdin: one argv string is capped at 128 KiB by Linux
  (E2BIG, seen on 2026-10-10 with a mission verification prompt).
"""

import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from ultron_hub.common.logging import get_logger
from ultron_hub.core.job import JobRequest, JobResult
from ultron_hub.core.registry import TaskSpec

log = get_logger("executor.claude_cli")

# Failure categories (JobResult.category). Only TRANSIENT is retried automatically.
AUTH, QUOTA, TRANSIENT, OTHER = "auth", "quota", "transient", "other"
DEFAULT_RETRY_DELAY_S = 30.0

_PATTERNS = (
    (AUTH, re.compile(r"not logged in|invalid[ _-]api[ _-]key|invalid x-api-key|authentication_error|"
                      r"oauth token (?:has )?expired|/login\b|\b401\b|unauthorized")),
    (QUOTA, re.compile(r"usage limit|rate[ _-]limit|\b429\b|limit reached|too many requests|quota")),
    (TRANSIENT, re.compile(r"econnreset|econnrefused|etimedout|enotfound|eai_again|epipe|socket hang up|"
                           r"network (?:error|is unreachable)|connection (?:error|reset|refused|closed)|"
                           r"fetch failed|timed? ?out|timeout|overloaded|temporarily unavailable|"
                           r"internal server error|bad gateway|service unavailable|gateway time-?out|"
                           r"\b5\d\d\b")),
)

FORBIDDEN_ARGS = ("--bare", "--dangerously-skip-permissions", "bypassPermissions")
_KILL_GRACE_S = 5
_TAIL = 2000


def resolve_claude_binary(configured: str | None = None) -> str:
    if configured:
        return configured
    found = shutil.which("claude", path=_clean_path())
    return found or str(Path.home() / ".local" / "bin" / "claude")


def _clean_path() -> str:
    return os.pathsep.join([str(Path.home() / ".local" / "bin"), "/usr/local/bin", "/usr/bin", "/bin"])


JOB_ENV = "ULTRON_HUB_JOB"


def clean_env() -> dict[str, str]:
    """Minimal environment: no inherited secrets, only what claude needs."""
    env = {
        "HOME": str(Path.home()),
        "PATH": _clean_path(),
        "LANG": os.environ.get("LANG") or "C.UTF-8",
        # Read by the user's Claude Code hooks (esprit context / journal summarize): a job never gets the
        # personal profile nor the journal, and its session is never summarized (no summary loop).
        JOB_ENV: "1",
    }
    if os.environ.get("XDG_RUNTIME_DIR"):  # lets read-only `systemctl --user status` reach the user manager
        env["XDG_RUNTIME_DIR"] = os.environ["XDG_RUNTIME_DIR"]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        env["CLAUDE_CONFIG_DIR"] = os.environ["CLAUDE_CONFIG_DIR"]
    return env


def _tail(text: str, size: int = _TAIL) -> str:
    text = text.strip()
    return text if len(text) <= size else "..." + text[-size:]


# Only these fields of a result object are searched: free text written by the agent (`result` of a long
# session, tool inputs in `permission_denials`) would turn "the test timed out" into a retried failure.
_ERROR_KEYS = ("error", "errors", "subtype", "terminal_reason")
_ERROR_HEAD = 300  # claude's own error messages are short and come first in `result`


def _error_fields(payload: dict) -> list[str]:
    """Error evidence of a result object: its error fields, its HTTP status, and the head of `result`."""
    out: list[str] = []

    def walk(node: Any) -> None:  # numbers are skipped: a cost of 0.429 is not an HTTP 429
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for key in _ERROR_KEYS:
        walk(payload.get(key))
    if isinstance(payload.get("api_error_status"), int):
        out.append(f"status {payload['api_error_status']}")
    if isinstance(payload.get("result"), str):
        out.append(payload["result"].strip()[:_ERROR_HEAD])
    return out


def _reported_errors(payload: dict) -> str:
    """claude's own reason when `result` is empty (todo #226: a budget stop read "exited with code 1")."""
    errors = payload.get("errors")
    if isinstance(errors, str):
        errors = [errors]
    reasons = [e.strip() for e in errors if isinstance(e, str) and e.strip()] if isinstance(errors, list) else []
    subtype = payload.get("subtype")
    if not reasons and isinstance(subtype, str) and subtype.startswith("error"):
        reasons = [subtype]
    return _tail("; ".join(reasons))


def classify_failure(*texts: str) -> str:
    """auth / quota / transient / other, from the result text, stderr and error fields; case-insensitive."""
    haystack = "\n".join(t for t in texts if t).lower()
    for category, pattern in _PATTERNS:
        if pattern.search(haystack):
            return category
    return OTHER


class ClaudeCliExecutor:
    def __init__(self, binary: str | None = None, disallowed_tools: tuple[str, ...] = (),
                 retry_delay_s: float = DEFAULT_RETRY_DELAY_S, sleep=time.sleep):
        self.binary = resolve_claude_binary(binary)
        self.disallowed_tools = tuple(disallowed_tools)
        self.retry_delay_s = max(0.0, float(retry_delay_s))
        self._sleep = sleep

    def build_command(self, spec: TaskSpec) -> list[str]:
        cmd = [
            self.binary,
            "-p",
            "--output-format", "json",
            "--model", spec.model,
            "--tools", ",".join(spec.tools),  # "" disables every tool
            "--permission-mode", "dontAsk",
            "--no-session-persistence",
        ]
        if spec.allowed_tools:
            cmd += ["--allowedTools", *spec.allowed_tools]
        if self.disallowed_tools:
            cmd += ["--disallowedTools", *self.disallowed_tools]
        if spec.add_dirs:
            cmd += ["--add-dir", *spec.add_dirs]
        if spec.settings is not None:
            cmd += ["--settings", json.dumps(spec.settings, separators=(",", ":"))]
        if spec.max_budget_usd is not None:
            cmd += ["--max-budget-usd", f"{spec.max_budget_usd:g}"]
        if spec.output_schema is not None:
            cmd += ["--json-schema", json.dumps(spec.output_schema, separators=(",", ":"))]
        bad = [a for a in cmd if a in FORBIDDEN_ARGS]
        if bad:
            raise RuntimeError(f"forbidden claude arguments: {bad}")
        return cmd

    def run(self, request: JobRequest, spec: TaskSpec, prompt: str, run_dir: Path,
            cwd: Path | None = None) -> JobResult:
        """`run_dir` receives the logs; `cwd` (default: run_dir) is where claude works.

        A `transient` failure (network, overloaded, 5xx) is retried once after `retry_delay_s`;
        auth, quota and other failures never are. The first attempt's logs are kept as *.try1.*.
        """
        result = self._run_once(request, spec, prompt, run_dir, cwd)
        if result.category != TRANSIENT:
            return result
        log.warning("job=%s transient failure (%s): retrying once in %.0fs", request.job_id,
                    _tail(result.error or result.summary, 200), self.retry_delay_s)
        for name, kept in (("stdout.json", "stdout.try1.json"), ("stderr.log", "stderr.try1.log")):
            if (run_dir / name).exists():
                (run_dir / name).replace(run_dir / kept)
        if self.retry_delay_s:
            self._sleep(self.retry_delay_s)
        retry = self._run_once(request, spec, prompt, run_dir, cwd)
        retry.cost_usd = (result.cost_usd or 0.0) + (retry.cost_usd or 0.0) if (
            result.cost_usd or retry.cost_usd) else None
        log.info("job=%s retry finished status=%s category=%s", request.job_id, retry.status, retry.category)
        return retry

    def _run_once(self, request: JobRequest, spec: TaskSpec, prompt: str, run_dir: Path,
                  cwd: Path | None) -> JobResult:
        run_dir.mkdir(parents=True, exist_ok=True)
        cmd = self.build_command(spec)
        base = {"job_id": request.job_id, "action": request.action, "model": spec.model}
        log.info("job=%s action=%s model=%s timeout=%ss start", request.job_id, spec.name, spec.model, spec.timeout_s)

        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd or run_dir,
                env=clean_env(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,  # own process group, killable as a whole
            )
        except OSError as exc:
            log.error("job=%s cannot start claude: %s", request.job_id, exc)
            return JobResult(**base, status="error", summary="claude could not be started",
                             error=f"cannot start {self.binary}: {exc}", duration_s=time.monotonic() - start,
                             category=OTHER)

        timed_out = False
        try:
            out_b, err_b = proc.communicate(prompt.encode("utf-8"), timeout=spec.timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            out_b, err_b = self._kill_group(proc)
        duration = time.monotonic() - start

        stdout = out_b.decode("utf-8", errors="replace")
        stderr = err_b.decode("utf-8", errors="replace")
        (run_dir / "stdout.json").write_text(stdout, encoding="utf-8")
        (run_dir / "stderr.log").write_text(stderr, encoding="utf-8")

        if timed_out:
            log.warning("job=%s timeout after %.1fs", request.job_id, duration)
            return JobResult(**base, status="timeout", summary=f"timed out after {spec.timeout_s:g}s",
                             error=f"claude did not finish within {spec.timeout_s:g}s", duration_s=duration,
                             category=OTHER)  # our own deadline: the task is too long, a retry would only double it

        result = self.parse_output(stdout, stderr, proc.returncode, spec, base, duration)
        log.info("job=%s status=%s rc=%s duration=%.1fs cost=%s",
                 request.job_id, result.status, proc.returncode, duration, result.cost_usd)
        return result

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> tuple[bytes, bytes]:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                pass
            try:
                return proc.communicate(timeout=_KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                continue
        return b"", b""

    @staticmethod
    def parse_output(stdout: str, stderr: str, returncode: int, spec: TaskSpec,
                     base: dict[str, Any], duration: float) -> JobResult:
        def fail(status: str, summary: str, error: str, cost: float | None = None,
                 evidence: list[str] | None = None) -> JobResult:
            """`evidence` replaces `error` for the classification when given (see _error_fields)."""
            seen = [error] if evidence is None else evidence
            return JobResult(**base, status=status, summary=summary, error=error, duration_s=duration,
                             cost_usd=cost, category=classify_failure(_tail(stderr), *seen))

        if not stdout.strip():
            detail = _tail(stderr) or "no stderr"
            return fail("error", "empty output from claude", f"empty stdout (exit code {returncode}): {detail}")

        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return fail("error", "unreadable output from claude",
                        f"invalid JSON on stdout (exit code {returncode}): {exc}", None, [stdout[:_ERROR_HEAD]])

        # Some versions emit a list of events; the final one has type == "result".
        if isinstance(payload, list):
            payload = next((e for e in reversed(payload) if isinstance(e, dict) and e.get("type") == "result"), None)
        if not isinstance(payload, dict):
            return fail("error", "unexpected output from claude", "JSON output is not a result object")

        fields = _error_fields(payload)
        cost = payload.get("total_cost_usd")
        cost = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
        text = payload.get("result") if isinstance(payload.get("result"), str) else ""
        denials = payload.get("permission_denials") or []
        is_error = payload.get("is_error")

        if not isinstance(is_error, bool):
            return fail("error", "unexpected output from claude", "missing or non-boolean `is_error`", cost, fields)

        if is_error or returncode != 0:
            message = (_tail(text) or _reported_errors(payload) or _tail(stderr)
                       or f"claude exited with code {returncode}")
            if denials:
                return fail("denied", f"permission denied: {_denied_tools(denials)}", message, cost)
            return fail("error", "claude reported an error", message, cost, fields)

        data = payload.get("structured_output")
        if data is None and text:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = None
        if spec.output_schema is not None and data is None:
            return fail("error", "no structured output", "expected `structured_output` matching the task schema", cost)

        if isinstance(data, dict) and isinstance(data.get("summary"), str):
            summary = data["summary"]
        else:
            summary = _tail(text, 500)
        if denials:
            summary = f"{summary} [permission denials: {_denied_tools(denials)}]".strip()

        return JobResult(**base, status="ok", summary=summary, data=data,
                         duration_s=duration, cost_usd=cost)


def _denied_tools(denials: list) -> str:
    names = [d.get("tool_name", "?") if isinstance(d, dict) else str(d) for d in denials]
    return ", ".join(sorted(set(names))) or "?"
