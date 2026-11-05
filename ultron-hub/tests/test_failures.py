"""Failure categories of `claude -p` and the single automatic retry of transient ones."""

import textwrap

import pytest
from test_claude_cli import SUCCESS, make_spec

from ultron_hub.core.job import JobRequest
from ultron_hub.executors import claude_cli
from ultron_hub.executors.claude_cli import AUTH, OTHER, QUOTA, TRANSIENT, ClaudeCliExecutor, classify_failure


def error_body(message: str, stderr: str = "", code: int = 1, extra: str = "") -> str:
    return textwrap.dedent(f"""
        sys.stderr.write({stderr!r})
        out({{"type": "result", "is_error": True, "result": {message!r}, "total_cost_usd": 0.429,
             "permission_denials": []{extra}}})
        sys.exit({code})
    """)


def execute(tmp_path, binary, delay=0.0, sleeps=None):
    sleep = (lambda s: sleeps.append(s)) if sleeps is not None else (lambda s: None)
    executor = ClaudeCliExecutor(binary=str(binary), retry_delay_s=delay, sleep=sleep)
    request = JobRequest(action="ping", job_id="test-job")
    run_dir = tmp_path / "runs" / request.job_id
    return executor.run(request, make_spec(tmp_path), "hello", run_dir), run_dir


@pytest.mark.parametrize("message, category", [
    ("Not logged in · Please run /login", AUTH),
    ("Invalid API key", AUTH),
    ("API Error: 401 unauthorized", AUTH),
    ("Claude AI usage limit reached", QUOTA),
    ("rate limit exceeded", QUOTA),
    ("API Error: 429 Too Many Requests", QUOTA),
    ("5-hour limit reached", QUOTA),
    ("API Error: 529 Overloaded", TRANSIENT),
    ("read ECONNRESET", TRANSIENT),
    ("request timed out", TRANSIENT),
    ("API Error: 503 Service Unavailable", TRANSIENT),
    ("Internal Server Error", TRANSIENT),
    ("fetch failed: network error", TRANSIENT),
    ("the model refused the task", OTHER),
])
def test_each_category_through_a_fake_claude(tmp_path, make_fake_claude, message, category):
    result, _ = execute(tmp_path, make_fake_claude(error_body(message)))
    assert result.status == "error" and result.category == category
    assert result.to_dict()["category"] == category


def test_category_case_insensitive_and_from_stderr(tmp_path, make_fake_claude):
    result, _ = execute(tmp_path, make_fake_claude(error_body("failed", stderr="NOT LOGGED IN")))
    assert result.category == AUTH


def test_category_from_json_error_field_not_from_numbers(tmp_path, make_fake_claude):
    # cost 0.429 and the word 429 inside a number must not look like an HTTP status
    other, _ = execute(tmp_path, make_fake_claude(error_body("boom")))
    assert other.category == OTHER
    nested, _ = execute(tmp_path, make_fake_claude(error_body("", extra=', "error": {"message": "Overloaded"}')))
    assert nested.category == TRANSIENT
    status, _ = execute(tmp_path, make_fake_claude(error_body("", extra=', "api_error_status": 429')))
    assert status.category == QUOTA


def test_empty_stdout_and_invalid_json_are_classified(tmp_path, make_fake_claude):
    empty, _ = execute(tmp_path, make_fake_claude("sys.stderr.write('connect ECONNREFUSED')\nsys.exit(1)\n"))
    assert empty.category == TRANSIENT
    garbage, _ = execute(tmp_path, make_fake_claude("print('Not logged in')\nsys.exit(1)\n"))
    assert garbage.category == AUTH


def test_success_and_timeout_have_no_retryable_category(tmp_path, make_fake_claude):
    ok, _ = execute(tmp_path, make_fake_claude(SUCCESS))
    assert ok.status == "ok" and ok.category is None
    executor = ClaudeCliExecutor(binary=str(make_fake_claude("time.sleep(30)\n")), retry_delay_s=0)
    timed = executor.run(JobRequest(action="ping", job_id="slow"), make_spec(tmp_path, timeout_s=0.5), "hi",
                         tmp_path / "runs" / "slow")
    assert timed.status == "timeout" and timed.category == OTHER


def test_classify_failure_priority():
    assert classify_failure("overloaded", "not logged in") == AUTH
    assert classify_failure("overloaded", "usage limit") == QUOTA
    assert classify_failure("", None) == OTHER


# --- retry ---------------------------------------------------------------------------------

def flaky(tmp_path, first: str, failures: int = 1) -> str:
    """Fails `failures` times with `first`, then succeeds; the number of calls is kept in a file."""
    counter = tmp_path / "calls"
    return textwrap.dedent(f"""
        import os
        p = {str(counter)!r}
        n = int(open(p).read()) if os.path.exists(p) else 0
        open(p, 'w').write(str(n + 1))
        if n < {failures}:
            out({{"type": "result", "is_error": True, "result": {first!r}, "permission_denials": []}})
            sys.exit(1)
    """) + SUCCESS


def calls(tmp_path) -> int:
    return int((tmp_path / "calls").read_text())


def test_transient_is_retried_once_then_succeeds(tmp_path, make_fake_claude, caplog):
    sleeps = []
    with caplog.at_level("WARNING"):
        result, run_dir = execute(tmp_path, make_fake_claude(flaky(tmp_path, "API Error: 529 Overloaded")),
                                  delay=7, sleeps=sleeps)
    assert result.status == "ok" and result.category is None and calls(tmp_path) == 2
    assert sleeps == [7]
    assert (run_dir / "stdout.try1.json").is_file() and (run_dir / "stdout.json").is_file()
    assert "retrying once" in caplog.text


def test_transient_is_retried_only_once(tmp_path, make_fake_claude):
    result, _ = execute(tmp_path, make_fake_claude(flaky(tmp_path, "ECONNRESET", failures=5)))
    assert result.status == "error" and result.category == TRANSIENT and calls(tmp_path) == 2


@pytest.mark.parametrize("message", ["Not logged in", "usage limit reached", "the model refused"])
def test_no_retry_for_auth_quota_other(tmp_path, make_fake_claude, message):
    sleeps = []
    result, _ = execute(tmp_path, make_fake_claude(flaky(tmp_path, message)), sleeps=sleeps)
    assert result.status == "error" and calls(tmp_path) == 1 and sleeps == []


def test_retry_delay_comes_from_settings(project):
    from ultron_hub.common.settings import load_settings
    assert load_settings().claude_retry_delay_s == 30
    (project / "config" / "settings.yaml").write_text("claude:\n  retry_delay_s: 0\n")
    assert load_settings().claude_retry_delay_s == 0
    assert claude_cli.DEFAULT_RETRY_DELAY_S == 30


def test_agent_text_is_not_searched_beyond_its_head(tmp_path, make_fake_claude):
    # A long final message from the agent mentioning a timeout or a 500 is not a network failure.
    prose = "J'ai corrigé le module. " * 40 + "Le test d'intégration a dépassé le timeout (HTTP 500)."
    result, _ = execute(tmp_path, make_fake_claude(error_body(prose)))
    assert result.category == OTHER


def test_denied_tool_inputs_are_not_searched(tmp_path, make_fake_claude):
    denials = ', "subtype": "error_max_turns", "permission_denials_x": [{"tool_input": {"command": "timeout 500 x"}}]'
    result, _ = execute(tmp_path, make_fake_claude(error_body("stopped", extra=denials)))
    assert result.category == OTHER
