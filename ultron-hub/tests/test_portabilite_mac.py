"""Todo #391: health probes without /proc (macOS) and the dashboard exit code that keeps launchd from looping."""

import argparse
from pathlib import Path

from ultron_hub.dashboard import auth, commands, server
from ultron_hub.health import collect as hc


def test_probe_system_without_proc_gives_unknown_values(tmp_path):
    res = hc.probe_system(tmp_path / "no-proc")
    assert res["uptime_s"] is None and res["booted_at"] is None
    assert res["cpus"] and res["hostname"]
    assert all(res[k] is None or isinstance(res[k], float) for k in ("load_1m", "load_5m", "load_15m"))  # getloadavg or None


def test_probe_memory_without_meminfo_gives_unknown_values(tmp_path):
    res = hc.probe_memory(tmp_path / "no-meminfo")
    assert set(res) == {"total_mib", "available_mib", "used_pct", "swap_total_mib", "swap_used_pct"}
    assert all(v is None for v in res.values())


def test_server_profile_and_collect_survive_without_proc(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "probe_memory", lambda *a, **k: dict.fromkeys(
        ("total_mib", "available_mib", "used_pct", "swap_total_mib", "swap_used_pct")))
    profile = hc.server_profile(tmp_path / "os-release", tmp_path / "cpuinfo")
    assert profile["ram_gib"] is None and profile["os"] is None


def _serve(monkeypatch, tmp_path, *flags):
    def no_config(port, path):
        raise auth.DashboardConfigError("liste d'appareils vide")
    monkeypatch.setattr(server, "serve", no_config)
    args = argparse.Namespace(dashboard_command="serve", port=8787, sans_config_code_0="--sans-config-code-0" in flags)
    return commands.handle(args)


def test_dashboard_without_config_exits_78_by_default(monkeypatch, tmp_path):
    assert _serve(monkeypatch, tmp_path)["exit_code"] == commands.EX_CONFIG == 78


def test_dashboard_without_config_exits_0_for_launchd(monkeypatch, tmp_path):
    res = _serve(monkeypatch, tmp_path, "--sans-config-code-0")
    assert res["exit_code"] == 0 and res["status"] == "error" and "vide" in res["error"]


def test_launchd_dashboard_template_uses_the_clean_exit():
    plist = (Path(__file__).parent.parent / "kit" / "gabarits" / "launchd" / "ultron-hub.dashboard.plist")
    if plist.is_file():  # not in the kit export
        assert "<string>--sans-config-code-0</string>" in plist.read_text(encoding="utf-8")
