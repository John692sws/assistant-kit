"""Todo #144: employé « tests » du ménage. La vraie suite n'est jamais lancée : faux pytest + faux junitxml."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_contract import check_argv
from ultron_hub import repos
from ultron_hub.menage import commands, tests_audit

JUNIT = """<?xml version="1.0"?><testsuites><testsuite>
<testcase classname="tests.test_a" name="test_one" time="0.5"/>
<testcase classname="tests.test_a" name="test_slow" time="3.5"/>
<testcase classname="tests.test_b" name="test_two" time="1.0"/>
<testcase classname="tests.test_b.TestK" name="test_bad" time="0.1"><failure message="x"/></testcase>
</testsuite></testsuites>"""


def make_settings(tmp_path):
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "tests" / "test_a.py").write_text("import time\ndef test_dup():\n    time.sleep(1)\n")
    (root / "tests" / "test_b.py").write_text(
        "def test_dup():\n    pass\nclass TestK:\n    def test_bad(self):\n        pass\n"
        "def test_ok(monkeypatch):\n    monkeypatch.setattr('time.sleep', lambda s: None)\n")
    return SimpleNamespace(root=root, var_dir=tmp_path / "var", repo_tests={}, todo_notify_target="telegram",
                           hermes_binary=None)


def fake_runner(junit=JUNIT, rc=0, hermes_calls=0, seen=None):
    def run(cmd, env, cwd, timeout_s):
        if seen is not None:
            seen.append((cmd, env, cwd, timeout_s))
        out = Path(next(a for a in cmd if a.startswith("--junitxml=")).split("=", 1)[1])
        if junit:
            out.write_text(junit)
        for _ in range(hermes_calls):
            subprocess.run([env["ULTRON_HUB_HERMES_BIN"], "send", "x"], check=True, env=env)
        return subprocess.CompletedProcess(cmd, rc, stdout="1 failed, 3 passed in 5.00s\n", stderr="")
    return run


def test_command_has_durations_and_junit_and_fake_hermes_first_on_path(tmp_path):
    s = make_settings(tmp_path)
    seen = []
    tests_audit.analyse(s, runner=fake_runner(seen=seen))
    cmd, env, cwd, timeout = seen[0]
    assert "--durations=0" in cmd and any(a.startswith("--junitxml=") for a in cmd) and "pytest" in cmd
    assert env["PATH"].split(":")[0].endswith("bin") and "menage-tests-" in env["PATH"]
    assert cwd == s.root and timeout == repos.TESTS_TIMEOUT_S


def test_measures_files_slow_sleeps_and_duplicates(tmp_path):
    data = tests_audit.analyse(make_settings(tmp_path), runner=fake_runner())
    assert data["tests"] == 4 and data["failed"] == 1 and data["ok"] is False
    assert data["files"][0] == {"file": "tests/test_a.py", "s": 4.0, "pct": 78}
    assert [s["id"] for s in data["slow"]] == ["tests/test_a.py::test_slow"]
    assert data["sleeps"] == {"test_a.py": 1}  # the monkeypatched sleep in test_b is not a call
    assert data["duplicates"] == {"test_dup": ["test_a.py", "test_b.py"]}
    assert data["new_slow"] == [] and data["hermes_calls"] == 0


def test_new_slow_tests_compared_with_history_and_history_written(tmp_path):
    s = make_settings(tmp_path)
    tests_audit.analyse(s, runner=fake_runner())
    more = JUNIT.replace("</testsuite>", '<testcase classname="tests.test_b" name="test_new" time="2.5"/></testsuite>')
    data = tests_audit.analyse(s, runner=fake_runner(junit=more))
    assert [x["id"] for x in data["new_slow"]] == ["tests/test_b.py::test_new"]
    rows = [json.loads(x) for x in (s.var_dir / "menage" / "tests-history.jsonl").read_text().splitlines()]
    assert len(rows) == 2 and rows[1]["tests"] == 5


def test_leak_to_fake_hermes_is_counted_and_warned(tmp_path):
    data = tests_audit.analyse(make_settings(tmp_path), runner=fake_runner(hermes_calls=2))
    assert data["hermes_calls"] == 2 and data["warn"] is True
    assert "fuite : 2 appel(s)" in "\n".join(tests_audit.lines(data))


def test_duration_warning_at_80_percent(tmp_path):
    s = make_settings(tmp_path)
    clean = JUNIT.replace("<failure", "<skipped").replace('time="3.5"', 'time="0.1"')
    assert tests_audit.analyse(s, runner=fake_runner(junit=clean))["warn"] is False
    ticks = iter([0.0, 0.83 * repos.TESTS_TIMEOUT_S])  # 83 % of the timeout
    data = tests_audit.analyse(s, runner=fake_runner(junit=clean), clock=lambda: next(ticks))
    assert data["warn"] is True and "⚠️ 🧪" in tests_audit.lines(data)[0]


def test_timeout_and_missing_junit_are_reported_not_raised(tmp_path):
    s = make_settings(tmp_path)

    def hang(cmd, env, cwd, timeout_s):
        raise subprocess.TimeoutExpired(cmd, timeout_s)

    data = tests_audit.analyse(s, runner=hang)
    assert data["ok"] is False and "délai max" in data["error"]
    data = tests_audit.analyse(s, runner=fake_runner(junit=None, rc=2))
    assert data["ok"] is False and "junitxml" in data["error"]
    assert not (s.var_dir / "menage" / "tests-history.jsonl").exists()  # nothing measured, no trend point


def test_thresholds_come_from_menage_yaml(tmp_path):
    s = make_settings(tmp_path)
    (s.root / "config" / "menage.yaml").write_text("tests:\n  slow_test_s: 0.2\n")
    data = tests_audit.analyse(s, runner=fake_runner())
    assert len(data["slow"]) == 3  # 0.5 s, 3.5 s and 1.0 s are above 0.2 s
    assert data["thresholds"]["warn_pct"] == 80


def test_real_config_has_a_tests_section():
    root = Path(__file__).resolve().parents[1]
    th = tests_audit.load_thresholds(SimpleNamespace(root=root))
    assert th["warn_pct"] == 80 and th["slow_test_s"] == 2.0


def test_the_real_suite_cannot_start_from_a_test(tmp_path):
    with pytest.raises(AssertionError):
        tests_audit.run_pytest(["true"], {}, tmp_path, 1)


def test_tests_command_sends_only_with_send(tmp_path, monkeypatch):
    s = make_settings(tmp_path)
    sent = []
    monkeypatch.setattr(commands.service, "send_message", lambda *a: sent.append(a))
    run = lambda settings: tests_audit.analyse(settings, runner=fake_runner())  # noqa: E731
    assert commands.do_tests(s, False, run)["sent"] is False and sent == []
    data = commands.do_tests(s, True, run)
    assert sent[0][0] == data["text"] and sent[0][1] == "telegram"


def test_cli_parses_menage_tests():
    check_argv(["menage", "tests"])
    check_argv(["menage", "tests", "--send"])


def test_weekly_report_gets_a_tests_section_and_survives_a_failing_suite(tmp_path, monkeypatch):
    s = make_settings(tmp_path)
    monkeypatch.setattr(commands.journal, "record", lambda *a: None)
    scan = {"anomalies": [], "counts": {}, "disk": [], "sizes": {}, "warnings": []}
    data = commands.do_report(s, False, scan=scan, tests_analyse=lambda st: tests_audit.analyse(st, runner=fake_runner()))
    assert "🧪 Tests" in data["text"] and "## Tests" in (s.var_dir / "menage" / "last.md").read_text()

    def boom(_):
        raise RuntimeError("x")

    data = commands.do_report(s, False, scan=scan, tests_analyse=boom)
    assert "Tout est rangé" in data["text"] and "section indisponible (RuntimeError)" in data["text"]
    assert "🧪" not in commands.do_report(s, False, scan=scan)["text"]  # no section unless asked
