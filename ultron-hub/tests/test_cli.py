import json

import pytest

from ultron_hub import cli
from ultron_hub.core.job import STATUSES

EXPECTED_KEYS = {"job_id", "action", "status", "summary", "data", "files", "error",
                 "model", "duration_s", "cost_usd", "category"}

SUCCESS = """
out({"is_error": False, "result": "", "structured_output": {"status": "ok", "summary": "pong"},
     "total_cost_usd": 0.001, "permission_denials": []})
"""


def run_cli(capsys, argv):
    code = cli.main(argv)
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert len(lines) == 1, f"stdout must hold exactly one JSON line, got: {captured.out!r}"
    payload = json.loads(lines[0])
    assert set(payload) == EXPECTED_KEYS
    assert payload["status"] in STATUSES
    return code, payload


def test_run_ping_ok(project, make_fake_claude, monkeypatch, capsys):
    monkeypatch.setenv("ULTRON_HUB_CLAUDE_BIN", str(make_fake_claude(SUCCESS)))
    code, out = run_cli(capsys, ["run", "ping", "--json", "--requested-by", "telegram:1",
                                 "--job-id", "job-1"])
    assert code == 0
    assert out["status"] == "ok" and out["summary"] == "pong" and out["model"] == "haiku"
    run_dir = project / "var" / "runs" / "job-1"
    for name in ("request.json", "result.json", "stdout.json", "stderr.log"):
        assert (run_dir / name).is_file(), name
    assert json.loads((run_dir / "request.json").read_text())["requested_by"] == "telegram:1"
    assert json.loads((run_dir / "result.json").read_text())["status"] == "ok"
    assert (project / "var" / "logs" / "ultron-hub.log").is_file()


def test_auto_job_id(project, make_fake_claude, monkeypatch, capsys):
    monkeypatch.setenv("ULTRON_HUB_CLAUDE_BIN", str(make_fake_claude(SUCCESS)))
    _, out = run_cli(capsys, ["run", "ping", "--json"])
    assert len(out["job_id"]) > 10 and (project / "var" / "runs" / out["job_id"]).is_dir()


def test_claude_error_gives_exit_1(project, make_fake_claude, monkeypatch, capsys):
    body = 'out({"is_error": True, "subtype": "success", "result": "Not logged in"})\n'
    monkeypatch.setenv("ULTRON_HUB_CLAUDE_BIN", str(make_fake_claude(body)))
    code, out = run_cli(capsys, ["run", "ping", "--json"])
    assert code == 1 and out["status"] == "error"


@pytest.mark.parametrize("argv, fragment", [
    (["run", "unknown", "--json"], "unknown action"),
    (["run", "ping", "--params", "{not json", "--json"], "not valid JSON"),
    (["run", "ping", "--params", "[1]", "--json"], "params must be"),
    (["run", "../x", "--json"], "invalid action"),
    (["run", "ping", "--job-id", "../../etc", "--json"], "invalid job_id"),
    (["bogus"], "invalid arguments"),
    ([], "invalid arguments"),
])
def test_errors_still_produce_json(project, capsys, argv, fragment):
    code, out = run_cli(capsys, argv)
    assert code == 1
    assert out["status"] == "error"
    assert fragment in out["error"]


def test_missing_param_is_json_error(project, capsys):
    (project / "tasks" / "ping" / "prompt.md").write_text("Ping {target}")
    code, out = run_cli(capsys, ["run", "ping", "--json"])
    assert code == 1 and "missing params" in out["error"]


def test_unexpected_exception_still_json(project, monkeypatch, capsys):
    def explode(*_args, **_kwargs):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(cli, "run_job", explode)
    code, out = run_cli(capsys, ["run", "ping", "--json"])
    assert code == 1 and "kaboom" in out["error"]
