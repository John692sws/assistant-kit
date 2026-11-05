import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import write_task
from hermes_plugin.health_flow import HealthFlow, cb, render_report
from hermes_plugin.hub_client import HubClient
from ultron_hub import cli
from ultron_hub.common.errors import TaskConfigError
from ultron_hub.core import collectors
from ultron_hub.core.registry import Registry
from ultron_hub.health import collect as hc
from ultron_hub.health.text import battery_text, duration_text
from cli_contract import check_argv

PROJECT_TASKS = Path(__file__).resolve().parents[1] / "tasks"
CHAT = 1185120603

UPOWER_BAT = """\
  native-path:          BAT0
  updated:              Sat 10 Oct 2026 09:35:07 AM UTC (12 seconds ago)
  battery
    state:               charging
    warning-level:       none
    energy:              19.1513 Wh
    energy-full:         45.9198 Wh
    energy-full-design:  54.7018 Wh
    energy-rate:         33.4949 W
    charge-cycles:       485
    time to full:        48.0 minutes
    percentage:          41.706%
    temperature:         32.8 degrees C
    capacity:            83.9457%
  History (charge):
    1791624907	41.706	charging
"""

BAT = "/org/freedesktop/UPower/devices/battery_BAT0"
ADP = "/org/freedesktop/UPower/devices/line_power_ADP1"


def fake_runner(outputs: dict[tuple, str], stderr: dict[tuple, str] | None = None):
    def run(cmd):
        key = tuple(cmd)
        return subprocess.CompletedProcess(cmd, 0, outputs.get(key, ""), (stderr or {}).get(key, ""))
    return run


def upower_runner(ac="yes", battery=UPOWER_BAT):
    return fake_runner({
        ("upower", "-e"): f"{BAT}\n{ADP}\n/org/freedesktop/UPower/devices/DisplayDevice\n",
        ("upower", "-i", ADP): f"  power supply:         yes\n  line-power\n    online:              {ac}\n",
        ("upower", "-i", BAT): battery,
    })


# --- collector ---------------------------------------------------------------

def test_probe_power():
    power = hc.probe_power(upower_runner())
    bat = power["battery"]
    assert power["ac_online"] is True
    assert bat["state"] == "charging" and bat["percentage"] == pytest.approx(41.706)
    assert bat["time_left"] == "48.0 minutes" and bat["time_left_kind"] == "to_full"
    assert bat["health_pct"] == pytest.approx(83.9457) and bat["charge_cycles"] == 485
    assert bat["energy_full_design_wh"] == pytest.approx(54.7018) and bat["temperature_c"] == 32.8


def test_probe_power_unplugged_and_no_battery():
    assert hc.probe_power(upower_runner(ac="no"))["ac_online"] is False
    power = hc.probe_power(fake_runner({("upower", "-e"): f"{ADP}\n", ("upower", "-i", ADP): "online: yes"}))
    assert power == {"ac_online": True, "battery": None}


def test_probe_memory(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 8000000 kB\nMemFree: 1 kB\nMemAvailable: 6000000 kB\n"
                       "SwapTotal: 4000000 kB\nSwapFree: 3000000 kB\n")
    mem = hc.probe_memory(meminfo)
    assert mem["used_pct"] == 25.0 and mem["swap_used_pct"] == 25.0 and mem["total_mib"] == 7812


def test_probe_disks_dedupes_same_filesystem(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    disks = hc.probe_disks((str(tmp_path), str(sub), "/does/not/exist"))
    assert [d["mount"] for d in disks] == [str(tmp_path)] and 0 <= disks[0]["used_pct"] <= 100


def test_probe_services():
    run = fake_runner({
        ("systemctl", "is-active", "tailscaled"): "active\n",
        ("systemctl", "--user", "is-active", "ultron-hub-dimanche.timer",
         "ultron-hub-heartbeat.timer", "ultron-hub-backup.timer"): "inactive\nactive\n",
        ("systemctl", "--user", "--failed", "--no-legend", "--plain"): "old.service not-found failed failed old\n",
    })
    services = hc.probe_services(run)
    assert services["system"] == {"tailscaled": "active"}
    assert services["user"] == {"ultron-hub-dimanche.timer": "inactive",
                                "ultron-hub-heartbeat.timer": "active", "ultron-hub-backup.timer": "unknown"}
    assert services["failed_system"] == [] and services["failed_user"] == ["old.service"]


def test_probe_updates(tmp_path):
    apt = tmp_path / "apt-check"
    apt.write_text("#!/bin/sh\n")
    apt.chmod(0o755)
    flag = tmp_path / "reboot-required"
    run = fake_runner({}, {(str(apt),): "22;3"})
    assert hc.probe_updates(run, str(apt), str(flag)) == {"pending": 22, "security": 3, "reboot_required": False}
    flag.touch()
    assert hc.probe_updates(run, str(tmp_path / "missing"), str(flag)) == \
        {"pending": None, "security": None, "reboot_required": True}


def test_collect_isolates_failing_probe(monkeypatch):
    def boom():
        raise OSError("no upower")
    monkeypatch.setattr(hc, "PROBES", {"power": boom, "memory": lambda: {"used_pct": 1}})
    metrics = hc.collect()
    assert metrics["power"] is None and metrics["memory"] == {"used_pct": 1}
    assert metrics["errors"] == {"power": "OSError: no upower"}
    assert "memory" not in hc.collect(only=("power",))


def test_cpu_temperature(tmp_path):
    for i, (kind, temp) in enumerate((("BAT0", "32900"), ("x86_pkg_temp", "51000"))):
        zone = tmp_path / f"thermal_zone{i}"
        zone.mkdir()
        (zone / "type").write_text(kind + "\n")
        (zone / "temp").write_text(temp + "\n")
    assert hc.cpu_temperature(tmp_path) == 51.0
    assert hc.cpu_temperature(tmp_path / "none") is None


def test_server_profile(tmp_path):
    osr, cpu = tmp_path / "os-release", tmp_path / "cpuinfo"
    osr.write_text('NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 24.04.5 LTS"\n')
    cpu.write_text("processor\t: 0\nmodel name\t: Intel i5\n")
    info = hc.server_profile(osr, cpu)
    assert info["os"] == "Ubuntu 24.04.5 LTS" and info["cpu"] == "Intel i5" and info["ram_gib"] > 0
    assert isinstance(info["nvidia_gpu"], bool)


# --- text ----------------------------------------------------------------------

def test_duration_text():
    assert duration_text("48.0 minutes") == "48 min"
    assert duration_text("1.2 hours") == "1 h 12"
    assert duration_text("weird") == "weird"


def test_battery_text():
    text = battery_text(hc.probe_power(upower_runner()))
    assert "🔌 Secteur : branché" in text and "42 % (en charge)" in text
    assert "Pleine dans : 48 min" in text and "Cycles : 485" in text and "54,7 Wh" in text
    assert "DÉBRANCHÉ" in battery_text(hc.probe_power(upower_runner(ac="no")))
    assert "Aucune batterie" in battery_text({"ac_online": True, "battery": None})
    assert "impossible" in battery_text(None)


def test_cli_health_battery(monkeypatch, capsys):
    monkeypatch.setattr(hc, "PROBES", {"power": lambda: hc.probe_power(upower_runner())})
    assert cli.main(["health", "battery"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["command"] == "health.battery" and out["summary"].startswith("🔋 Batterie : 42 %")
    assert out["data"]["battery"]["charge_cycles"] == 485 and "Santé" in out["data"]["text"]


def test_cli_health_battery_failure(monkeypatch, capsys):
    def boom():
        raise OSError("no upower")
    monkeypatch.setattr(hc, "PROBES", {"power": boom})
    assert cli.main(["health", "battery"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error" and "no upower" in out["error"]


# --- task, registry and runner --------------------------------------------------

def test_real_sante_task_loads():
    spec = Registry(PROJECT_TASKS).load("sante-serveur")
    assert spec.model == "haiku" and spec.tools == () and spec.collect == "health"
    assert "{metrics}" in spec.prompt_template
    assert spec.output_schema["properties"]["level"]["enum"] == ["ok", "warn", "critical"]


def test_unknown_collector_rejected(tmp_path):
    write_task(tmp_path, "bad", "name: bad\ncollect: rm-rf\n")
    with pytest.raises(TaskConfigError, match="unknown collector"):
        Registry(tmp_path).load("bad")


def test_run_inserts_collected_metrics(project, make_fake_claude, read_argv, monkeypatch, capsys):
    monkeypatch.setitem(collectors.COLLECTORS, "health", lambda: {"disk": 42})
    write_task(project / "tasks", "check", "name: check\nmodel: haiku\ncollect: health\n",
               prompt="Metrics: {metrics}")
    body = 'out({"is_error": False, "result": "", "structured_output": {"status": "ok", "summary": "fine"}})\n'
    monkeypatch.setenv("ULTRON_HUB_CLAUDE_BIN", str(make_fake_claude(body)))
    assert cli.main(["run", "check", "--job-id", "job-c", "--params", '{"metrics": "spoofed"}']) == 0
    capsys.readouterr()
    assert read_argv()["stdin"] == 'Metrics: {"disk": 42}'
    assert json.loads((project / "var" / "runs" / "job-c" / "collected.json").read_text()) == {"disk": 42}


# --- Telegram flow -----------------------------------------------------------------

REPORT = {
    "status": "ok", "summary": "Coupure probable", "model": "haiku", "duration_s": 12.4, "cost_usd": 0.0063,
    "data": {"level": "warn", "summary": "Coupure probable ce matin.",
             "points": [{"level": "warn", "label": "Batterie", "detail": "46 % en charge"},
                        {"level": "ok", "label": "Disque", "detail": "40 %"}],
             "recommendations": ["Vérifier le chargeur"]},
}


class FakeClient:
    def __init__(self, result=REPORT):
        self.calls = []
        self.result = result
        self.gate: asyncio.Event | None = None
        self.guard_problem: str | None = None

    async def call(self, *args, timeout_s=None):
        check_argv(args)
        if args[:2] == ("health", "guard"):
            return {"status": "ok", "data": {"ok": not self.guard_problem, "problem": self.guard_problem}}
        self.calls.append((args, timeout_s))
        if args[:2] == ("health", "battery"):
            return {"status": "ok", "data": {"text": "🔋 Batterie : 46 %"}}
        if self.gate is not None:
            await self.gate.wait()
        return self.result


def run(coro):
    return asyncio.run(coro)


def callbacks(screen):
    return [data for row in screen.buttons for _, data in row]


def test_callback_data_fits_telegram_limit():
    for verb in ("full", "bat", "menu"):
        assert len(cb(verb).encode()) <= 64


def test_menu_and_battery():
    flow = HealthFlow(FakeClient())
    assert set(callbacks(run(flow.on_command(CHAT)))) == {cb("full"), cb("bat"), "hub:home"}
    assert "46 %" in run(flow.on_callback(CHAT, cb("bat"))).text


def test_full_analysis_shows_progress_then_report():
    flow, shown = HealthFlow(FakeClient()), []

    async def progress(screen):
        shown.append(screen.text)

    screen = run(flow.on_callback(CHAT, cb("full"), progress))
    assert shown and shown[0].startswith("⏳")
    assert "🟠 À surveiller" in screen.text and "🟠 Batterie — 46 % en charge" in screen.text
    assert "• Vérifier le chargeur" in screen.text and "haiku" in screen.text and "0,006 $" in screen.text
    args, timeout = flow.client.calls[-1]
    assert args == ("run", "sante-serveur", "--requested-by", f"telegram:{CHAT}") and timeout >= 90
    assert CHAT not in flow.running


def test_double_tap_runs_once():
    async def scenario():
        flow = HealthFlow(FakeClient())
        flow.client.gate = asyncio.Event()
        first = asyncio.create_task(flow.on_callback(CHAT, cb("full")))
        await asyncio.sleep(0)
        second = await flow.on_callback(CHAT, cb("full"))
        flow.client.gate.set()
        return flow, await first, second

    flow, first, second = run(scenario())
    assert "déjà en cours" in second.text and "Santé serveur" in first.text
    assert len(flow.client.calls) == 1


def test_full_analysis_failures():
    flow = HealthFlow(FakeClient({"status": "timeout", "error": "slow"}))
    assert "délai" in run(flow.on_callback(CHAT, cb("full"))).text
    flow = HealthFlow(FakeClient({"status": "error", "error": "Not logged in"}))
    screen = run(flow.on_callback(CHAT, cb("full")))
    assert screen.text.startswith("❌") and "Not logged in" in screen.text and CHAT not in flow.running


def test_render_report_is_bounded():
    long = {**REPORT, "data": {**REPORT["data"], "points": [{"level": "ok", "label": "x", "detail": "y" * 500}] * 20}}
    assert len(render_report(long)) <= 4000


def test_client_timeout_override(tmp_path):
    path = tmp_path / "ultron-hub"
    path.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(5)\n")
    path.chmod(0o755)
    out = run(HubClient(path, timeout_s=60).call("run", "x", timeout_s=0.5))
    assert out["status"] == "error" and "délai" in out["error"]


# --- ultron-guard probe, history, alert -----------------------------------------

def hermes_home(tmp_path, enabled=("ultron-guard", "ultron-hub"), plugin=True, config=True):
    home = tmp_path / "hermes"
    (home / "plugins" / "ultron-guard").mkdir(parents=True)
    if plugin:
        (home / "plugins" / "ultron-guard" / "__init__.py").write_text("")
    if config:
        (home / "config.yaml").write_text("plugins:\n  enabled:\n" + "".join(f"    - {n}\n" for n in enabled))
    return home


def test_probe_guard(tmp_path):
    assert hc.probe_guard(hermes_home(tmp_path)) == {"enabled_in_config": True, "plugin_file": True, "ok": True,
                                                    "problem": None}


def test_probe_guard_detects_each_failure(tmp_path):
    disabled = hc.probe_guard(hermes_home(tmp_path / "a", enabled=("ultron-hub",)))
    assert not disabled["ok"] and "plugins.enabled" in disabled["problem"]
    nofile = hc.probe_guard(hermes_home(tmp_path / "b", plugin=False))
    assert not nofile["ok"] and "manquant" in nofile["problem"]
    noconf = hc.probe_guard(hermes_home(tmp_path / "c", config=False))
    assert not noconf["ok"] and "illisible" in noconf["problem"]
    broken = hermes_home(tmp_path / "d")
    (broken / "config.yaml").write_text("plugins: [unclosed\n")
    assert not hc.probe_guard(broken)["ok"]
    (broken / "config.yaml").write_text("- just\n- a list\n")
    assert not hc.probe_guard(broken)["ok"]
    (broken / "config.yaml").write_text("plugins:\n  enabled: null\n")
    assert not hc.probe_guard(broken)["ok"]


def test_history_is_truncated_and_summarised(tmp_path):
    path = tmp_path / "health" / "history.jsonl"
    for i in range(5):
        metrics = {"collected_at": f"2026-10-11T0{i}:00:00+00:00",
                   "power": {"ac_online": i != 2, "battery": {"percentage": 80 - i, "state": "charging"}},
                   "memory": {"used_pct": 10 + i}, "disks": [{"used_pct": 30.0}, {"used_pct": 40.5}],
                   "services": {"failed_system": [], "failed_user": ["x"] if i == 4 else []},
                   "guard": {"ok": i != 3}}
        hc.record_history(metrics, path, max_lines=3)
    assert len(path.read_text().splitlines()) == 3
    digest = hc.probe_history(path)
    assert digest["samples"] == 3 and digest["since"].startswith("2026-10-11T02")
    assert digest["on_battery_samples"] == 1 and digest["battery_pct_min"] == 76
    assert digest["disk_used_pct_max"] == 40.5 and digest["samples_with_failed_units"] == 1
    assert digest["samples_guard_not_ok"] == 1
    path.write_text(path.read_text() + "{torn\n")
    assert hc.probe_history(path)["samples"] == 3
    assert hc.probe_history(tmp_path / "none.jsonl") == {"samples": 0}


def test_cli_collect_record(project, monkeypatch, capsys):
    monkeypatch.setattr(hc, "PROBES", {"memory": lambda: {"used_pct": 5}})
    assert cli.main(["health", "collect", "--record"]) == 0
    capsys.readouterr()
    assert len((project / "var" / "health" / "history.jsonl").read_text().splitlines()) == 1
    assert cli.main(["health", "collect"]) == 0
    assert len((project / "var" / "health" / "history.jsonl").read_text().splitlines()) == 1


def test_cli_health_guard(monkeypatch, capsys):
    monkeypatch.setattr(hc, "PROBES", {"guard": lambda: {"ok": False, "problem": "désactivé"}})
    assert cli.main(["health", "guard"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["data"]["problem"] == "désactivé" and "NON chargé" in out["summary"]


def test_sante_screens_warn_when_guard_missing():
    flow = HealthFlow(FakeClient())
    flow.client.guard_problem = "ultron-guard absent de plugins.enabled"
    assert run(flow.on_command(CHAT)).text.startswith("🚨 ultron-guard n'est PAS chargé")
    assert "🚨" in run(flow.on_callback(CHAT, cb("menu"))).text
    full = run(flow.on_callback(CHAT, cb("full"))).text
    assert full.startswith("🚨") and "Santé serveur" in full
    flow.client.guard_problem = None
    assert "🚨" not in run(flow.on_command(CHAT)).text


def test_health_service_records_history():
    unit = (Path(__file__).resolve().parents[1] / "deploy/systemd/ultron-hub-health.service").read_text()
    assert "health collect --record" in unit
