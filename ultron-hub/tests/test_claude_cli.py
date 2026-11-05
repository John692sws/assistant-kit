import json
import os
import time

import pytest

from conftest import PING_SCHEMA
from ultron_hub.core.job import JobRequest
from ultron_hub.core.registry import TaskSpec
from ultron_hub.executors.claude_cli import ClaudeCliExecutor


def make_spec(tmp_path, **overrides) -> TaskSpec:
    values = dict(
        name="ping", description="", model="haiku", timeout_s=10, tools=(), allowed_tools=(),
        add_dirs=(), prompt_template="hi", output_schema=PING_SCHEMA, path=tmp_path,
    )
    values.update(overrides)
    return TaskSpec(**values)


def run(tmp_path, binary, **spec_overrides):
    spec = make_spec(tmp_path, **spec_overrides)
    request = JobRequest(action="ping", job_id="test-job")
    run_dir = tmp_path / "runs" / request.job_id
    return ClaudeCliExecutor(binary=str(binary)).run(request, spec, "hello", run_dir), run_dir


SUCCESS = """
out({"type": "result", "subtype": "success", "is_error": False, "result": "",
     "structured_output": {"status": "ok", "summary": "pong"},
     "total_cost_usd": 0.0012, "permission_denials": []})
"""


def test_success_with_structured_output(tmp_path, make_fake_claude, read_argv):
    result, run_dir = run(tmp_path, make_fake_claude(SUCCESS))
    assert result.status == "ok"
    assert result.data == {"status": "ok", "summary": "pong"}
    assert result.summary == "pong"
    assert result.cost_usd == pytest.approx(0.0012)
    assert result.model == "haiku"
    assert result.error is None
    assert (run_dir / "stdout.json").is_file() and (run_dir / "stderr.log").is_file()
    # Dedicated cwd and clean env.
    recorded = read_argv()
    assert recorded["cwd"] == str(run_dir)
    assert set(recorded["env"]) <= {"HOME", "PATH", "LANG", "XDG_RUNTIME_DIR", "CLAUDE_CONFIG_DIR", "ULTRON_HUB_JOB"}
    assert ".local/bin" in recorded["env"]["PATH"]


def test_command_line_is_safe(tmp_path, make_fake_claude, read_argv):
    run(tmp_path, make_fake_claude(SUCCESS))
    argv = read_argv()["argv"][1:]
    assert "-p" in argv
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--tools") + 1] == ""
    assert "--no-session-persistence" in argv
    assert json.loads(argv[argv.index("--json-schema") + 1]) == PING_SCHEMA
    assert "hello" not in argv and "--" not in argv
    assert read_argv()["stdin"] == "hello"  # prompt via stdin, never argv
    for forbidden in ("--bare", "--dangerously-skip-permissions", "bypassPermissions"):
        assert forbidden not in argv


def test_build_command_options(tmp_path):
    spec = make_spec(tmp_path, model="sonnet", tools=("Read", "Grep"), allowed_tools=("Read",),
                     add_dirs=("/srv/data",), output_schema=None)
    cmd = ClaudeCliExecutor(binary="/x/claude", disallowed_tools=("Bash(sudo:*)",)).build_command(spec)
    assert cmd[0] == "/x/claude"
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert cmd[cmd.index("--tools") + 1] == "Read,Grep"
    assert cmd[cmd.index("--allowedTools") + 1] == "Read"
    assert cmd[cmd.index("--disallowedTools") + 1] == "Bash(sudo:*)"
    assert cmd[cmd.index("--add-dir") + 1] == "/srv/data"
    assert "--json-schema" not in cmd
    assert "--bare" not in cmd
    assert cmd[-2:] == ["--add-dir", "/srv/data"]


def test_budget_and_separate_cwd(tmp_path, make_fake_claude, read_argv):
    work = tmp_path / "work"
    work.mkdir()
    spec = make_spec(tmp_path, max_budget_usd=1.5)
    request = JobRequest(action="ping", job_id="test-job")
    run_dir = tmp_path / "runs" / "step"
    ClaudeCliExecutor(binary=str(make_fake_claude(SUCCESS))).run(request, spec, "hello", run_dir, cwd=work)
    recorded = read_argv()
    assert recorded["cwd"] == str(work) and (run_dir / "stdout.json").is_file()
    argv = recorded["argv"]
    assert argv[argv.index("--max-budget-usd") + 1] == "1.5"


def test_large_prompt_goes_through_stdin(tmp_path, make_fake_claude, read_argv):
    # One argv string is capped at 128 KiB (E2BIG): a 300 KB prompt must still start.
    spec = make_spec(tmp_path)
    prompt = "x" * 300_000
    request = JobRequest(action="ping", job_id="test-job")
    result = ClaudeCliExecutor(binary=str(make_fake_claude(SUCCESS))).run(request, spec, prompt, tmp_path / "run")
    assert result.status == "ok"
    assert read_argv()["stdin"] == prompt


def test_build_command_rejects_forbidden_args(tmp_path):
    spec = make_spec(tmp_path, allowed_tools=("--bare",))
    with pytest.raises(RuntimeError):
        ClaudeCliExecutor(binary="/x/claude").build_command(spec)


def test_is_error_true_with_subtype_success_is_an_error(tmp_path, make_fake_claude):
    body = """
out({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in",
     "total_cost_usd": 0, "permission_denials": []})
"""
    result, _ = run(tmp_path, make_fake_claude(body))
    assert result.status == "error"
    assert "Not logged in" in result.error


def test_budget_stop_gives_claude_reason(tmp_path, make_fake_claude):
    # Real output of the 15/10 plan (todo #226): empty result, empty stderr, the reason is only in `errors`.
    body = """
out({"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "errors": ["Reached maximum budget ($1)"],
     "terminal_reason": "budget_exhausted", "total_cost_usd": 1.15, "permission_denials": []})
sys.exit(1)
"""
    result, _ = run(tmp_path, make_fake_claude(body))
    assert result.status == "error"
    assert "Reached maximum budget ($1)" in result.error
    assert "exited with code" not in result.error


def test_error_subtype_used_when_no_errors_list(tmp_path, make_fake_claude):
    body = """
out({"type": "result", "subtype": "error_max_turns", "is_error": True, "total_cost_usd": 0, "permission_denials": []})
sys.exit(1)
"""
    result, _ = run(tmp_path, make_fake_claude(body))
    assert "error_max_turns" in result.error


def test_nonzero_exit_code_is_an_error(tmp_path, make_fake_claude):
    body = """
out({"type": "result", "subtype": "success", "is_error": False, "result": "",
     "structured_output": {"status": "ok", "summary": "pong"}})
sys.exit(3)
"""
    result, _ = run(tmp_path, make_fake_claude(body))
    assert result.status == "error"


def test_empty_output(tmp_path, make_fake_claude):
    result, _ = run(tmp_path, make_fake_claude("sys.stderr.write('boom')\nsys.exit(1)\n"))
    assert result.status == "error"
    assert "empty stdout" in result.error and "boom" in result.error


def test_invalid_json(tmp_path, make_fake_claude):
    result, _ = run(tmp_path, make_fake_claude("print('not json {')\n"))
    assert result.status == "error"
    assert "invalid JSON" in result.error


def test_missing_structured_output_with_schema(tmp_path, make_fake_claude):
    body = 'out({"is_error": False, "result": "plain text"})\n'
    result, _ = run(tmp_path, make_fake_claude(body))
    assert result.status == "error"
    assert "structured_output" in result.error


def test_timeout_kills_process_group(tmp_path, make_fake_claude):
    body = """
import subprocess
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(os.path.join(os.path.dirname(sys.argv[0]), "child.pid"), "w").write(str(child.pid))
time.sleep(60)
"""
    script = make_fake_claude(body)
    start = time.monotonic()
    result, _ = run(tmp_path, script, timeout_s=1)
    assert result.status == "timeout"
    assert time.monotonic() - start < 15
    child_pid = int((script.parent / "child.pid").read_text())
    with pytest.raises(ProcessLookupError):
        for _ in range(50):  # give the kernel a moment to reap
            os.kill(child_pid, 0)
            time.sleep(0.1)


def test_permission_denials_cause_denied(tmp_path, make_fake_claude):
    body = """
out({"is_error": True, "subtype": "success", "result": "I could not read the file",
     "permission_denials": [{"tool_name": "Read", "tool_input": {"file_path": "/x"}}]})
"""
    result, _ = run(tmp_path, make_fake_claude(body))
    assert result.status == "denied"
    assert "Read" in result.summary


def test_permission_denials_reported_on_success(tmp_path, make_fake_claude):
    body = """
out({"is_error": False, "result": "", "structured_output": {"status": "warn", "summary": "partial"},
     "permission_denials": [{"tool_name": "Bash"}]})
"""
    result, _ = run(tmp_path, make_fake_claude(body))
    assert result.status == "ok"
    assert "partial" in result.summary and "permission denials: Bash" in result.summary


def test_missing_binary(tmp_path):
    result, _ = run(tmp_path, tmp_path / "does-not-exist")
    assert result.status == "error"
    assert "cannot start" in result.error
