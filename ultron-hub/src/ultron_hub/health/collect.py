"""Server metrics collected by plain code (never by Claude).

Each probe is independent: a failing probe is reported in `errors` and the
others still run. Commands run with a minimal environment and a short timeout.
"""

import json
import os
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from ultron_hub.common.settings import project_root

CMD_TIMEOUT_S = 10
DISK_MOUNTS = ("/", "/home")
SYSTEM_UNITS = ("tailscaled",)
USER_UNITS: tuple[str, ...] = ()  # hermes-gateway disabled since 18/10: no longer watched
USER_TIMERS = ("ultron-hub-dimanche.timer", "ultron-hub-heartbeat.timer", "ultron-hub-backup.timer")  # todo #460: dimanche replaces todo-digest
APT_CHECK = "/usr/lib/update-notifier/apt-check"
REBOOT_REQUIRED = "/var/run/reboot-required"
THERMAL_DIR = Path("/sys/class/thermal")
CPU_THERMAL_TYPES = ("x86_pkg_temp", "coretemp", "cpu-thermal", "k10temp")

HERMES_HOME = Path.home() / ".hermes"
GUARD_PLUGIN = "ultron-guard"
HISTORY_MAX_LINES = 336  # one sample per 30 min -> 7 days

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _env() -> dict[str, str]:
    # systemctl --user needs the user's runtime dir to reach its manager.
    return {"HOME": str(Path.home()), "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
            "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"}


def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=CMD_TIMEOUT_S, env=_env(),
                          stdin=subprocess.DEVNULL, check=False)


def _number(text: str) -> float | None:
    try:
        return float(text.split()[0].rstrip("%"))
    except (IndexError, ValueError):
        return None


# -- probes ------------------------------------------------------------------

def probe_system(proc: Path = Path("/proc")) -> dict[str, Any]:
    """Uptime and load. Without /proc (macOS) the unknown values are None, never an exception."""
    try:
        uptime_s = float((proc / "uptime").read_text().split()[0])
    except (OSError, IndexError, ValueError):
        uptime_s = None
    try:
        load = [float(x) for x in (proc / "loadavg").read_text().split()[:3]]
        if len(load) != 3:
            raise ValueError
    except (OSError, ValueError):
        try:
            load = list(os.getloadavg())
        except OSError:
            load = [None, None, None]
    boot = datetime.fromtimestamp(time.time() - uptime_s, timezone.utc) if uptime_s is not None else None
    return {
        "hostname": socket.gethostname(),
        "uptime_s": int(uptime_s) if uptime_s is not None else None,
        "booted_at": boot.isoformat(timespec="seconds") if boot else None,
        "cpus": os.cpu_count(),
        "load_1m": load[0], "load_5m": load[1], "load_15m": load[2],
        "cpu_temp_c": cpu_temperature(),
    }


def cpu_temperature(thermal_dir: Path = THERMAL_DIR) -> float | None:
    for zone in sorted(thermal_dir.glob("thermal_zone*")):
        try:
            if zone.joinpath("type").read_text().strip() in CPU_THERMAL_TYPES:
                return int(zone.joinpath("temp").read_text().strip()) / 1000
        except (OSError, ValueError):
            continue
    return None


def probe_memory(meminfo: Path = Path("/proc/meminfo")) -> dict[str, Any]:
    """Memory figures. Without /proc/meminfo (macOS) every value is None (unknown), never an exception."""
    kb = {}
    try:
        for line in meminfo.read_text().splitlines():
            key, _, value = line.partition(":")
            if value.strip():
                kb[key] = int(value.split()[0])
        total, avail = kb["MemTotal"], kb["MemAvailable"]
    except (OSError, KeyError, ValueError, IndexError):
        return {"total_mib": None, "available_mib": None, "used_pct": None,
                "swap_total_mib": None, "swap_used_pct": None}
    mib = lambda k: round(kb.get(k, 0) / 1024)  # noqa: E731
    swap_total, swap_free = kb.get("SwapTotal", 0), kb.get("SwapFree", 0)
    return {
        "total_mib": mib("MemTotal"), "available_mib": mib("MemAvailable"),
        "used_pct": round(100 * (total - avail) / total, 1),
        "swap_total_mib": mib("SwapTotal"),
        "swap_used_pct": round(100 * (swap_total - swap_free) / swap_total, 1) if swap_total else 0.0,
    }


def probe_disks(mounts: tuple[str, ...] = DISK_MOUNTS) -> list[dict[str, Any]]:
    disks, seen = [], set()
    for mount in mounts:
        if not os.path.isdir(mount):
            continue
        dev = os.stat(mount).st_dev
        if dev in seen:  # /home on the same filesystem as /
            continue
        seen.add(dev)
        usage = shutil.disk_usage(mount)
        gib = 1024 ** 3
        disks.append({"mount": mount, "total_gib": round(usage.total / gib, 1),
                      "free_gib": round(usage.free / gib, 1),
                      # Same ratio as `df`: the root-reserved blocks count as unavailable.
                      "used_pct": round(100 * usage.used / (usage.used + usage.free), 1)})
    return disks


def parse_upower(text: str) -> dict[str, str]:
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition(":")
        if sep and value.strip() and key not in fields:
            fields[key.strip()] = value.strip()
    return fields


def probe_power(run: Runner = run_cmd) -> dict[str, Any]:
    devices = run(["upower", "-e"]).stdout.split()
    battery_dev = next((d for d in devices if "battery_" in d), None)
    line_dev = next((d for d in devices if "line_power" in d), None)

    ac_online = None
    if line_dev:
        online = parse_upower(run(["upower", "-i", line_dev]).stdout).get("online")
        ac_online = {"yes": True, "no": False}.get(online)
    if battery_dev is None:
        return {"ac_online": ac_online, "battery": None}

    f = parse_upower(run(["upower", "-i", battery_dev]).stdout)
    time_left = f.get("time to full") or f.get("time to empty")
    battery = {
        "state": f.get("state"),
        "percentage": _number(f.get("percentage", "")),
        "time_left": time_left,
        "time_left_kind": "to_full" if "time to full" in f else ("to_empty" if "time to empty" in f else None),
        "health_pct": _number(f.get("capacity", "")),
        "energy_wh": _number(f.get("energy", "")),
        "energy_full_wh": _number(f.get("energy-full", "")),
        "energy_full_design_wh": _number(f.get("energy-full-design", "")),
        "rate_w": _number(f.get("energy-rate", "")),
        "charge_cycles": int(_number(f["charge-cycles"]) or 0) if "charge-cycles" in f else None,
        "temperature_c": _number(f.get("temperature", "")),
        "warning_level": f.get("warning-level"),
    }
    return {"ac_online": ac_online, "battery": battery}


def probe_services(run: Runner = run_cmd) -> dict[str, Any]:
    from ultron_hub.common import plateforme
    if plateforme.est_mac():  # todo #390: no systemd; the kit's launchd agents, by their systemd unit name
        user = {plateforme.unite(nom): plateforme.etat(nom) for nom in plateforme.SERVICES}
        return {"system": {}, "user": user, "failed_system": [],
                "failed_user": sorted(u for u, s in user.items() if s == "failed")}

    def states(args: list[str], units: tuple[str, ...]) -> dict[str, str]:
        out = run(["systemctl", *args, "is-active", *units]).stdout.split()
        return {u: (out[i] if i < len(out) else "unknown") for i, u in enumerate(units)}

    def failed(args: list[str]) -> list[str]:
        out = run(["systemctl", *args, "--failed", "--no-legend", "--plain"]).stdout
        return [line.split()[0] for line in out.splitlines() if line.strip()]

    return {
        "system": states([], SYSTEM_UNITS),
        "user": states(["--user"], USER_UNITS + USER_TIMERS),
        "failed_system": failed([]),
        "failed_user": failed(["--user"]),
    }


def probe_updates(run: Runner = run_cmd, apt_check: str = APT_CHECK,
                  reboot_flag: str = REBOOT_REQUIRED) -> dict[str, Any]:
    pending = security = None
    if os.access(apt_check, os.X_OK):
        out = run([apt_check]).stderr.strip()  # apt-check prints "<updates>;<security>" on stderr
        parts = out.split(";")
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            pending, security = int(parts[0]), int(parts[1])
    return {"pending": pending, "security": security, "reboot_required": os.path.exists(reboot_flag)}


def server_profile(os_release: Path = Path("/etc/os-release"), cpuinfo: Path = Path("/proc/cpuinfo")) -> dict:
    """Static facts about the machine, for planners (no secrets, no usage figures)."""
    def first(path: Path, key: str) -> str | None:
        try:
            for line in path.read_text().splitlines():
                name, sep, value = line.partition("=" if path == os_release else ":")
                if sep and name.strip() == key:
                    return value.strip().strip('"')
        except OSError:
            pass
        return None

    memory = probe_memory()
    disks = probe_disks()
    return {
        "os": first(os_release, "PRETTY_NAME"),
        "cpu": first(cpuinfo, "model name"),
        "cpus": os.cpu_count(),
        "ram_gib": round(memory["total_mib"] / 1024, 1) if memory["total_mib"] is not None else None,
        "disk_free_gib": disks[0]["free_gib"] if disks else None,
        "nvidia_gpu": shutil.which("nvidia-smi") is not None,
        "laptop_on_battery_backup": Path("/sys/class/power_supply/BAT0").exists(),
    }


def probe_guard(hermes_home: Path | None = None) -> dict[str, Any]:
    """Read-only check that the ultron-guard Hermes plugin is enabled and present on disk."""
    home = hermes_home or HERMES_HOME
    plugin_file = home / "plugins" / GUARD_PLUGIN / "__init__.py"
    try:
        config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
        enabled = ((config.get("plugins") or {}).get("enabled") or []) if isinstance(config, dict) else []
        in_config = isinstance(enabled, list) and GUARD_PLUGIN in enabled
        config_error = None
    except (OSError, yaml.YAMLError) as exc:
        in_config, config_error = False, f"config.yaml illisible ({type(exc).__name__})"
    file_present = plugin_file.is_file()
    problems = []
    if config_error:
        problems.append(config_error)
    elif not in_config:
        problems.append(f"{GUARD_PLUGIN} absent de plugins.enabled")
    if not file_present:
        problems.append(f"{plugin_file.name} du plugin manquant")
    return {"enabled_in_config": in_config, "plugin_file": file_present, "ok": not problems,
            "problem": ", ".join(problems) or None}


def history_path(root: Path | None = None) -> Path:
    return (root or project_root()) / "var" / "health" / "history.jsonl"


def snapshot(metrics: dict[str, Any]) -> dict[str, Any]:
    """Compact sample kept in the history (figures only)."""
    power, memory = metrics.get("power") or {}, metrics.get("memory") or {}
    battery = power.get("battery") or {}
    services = metrics.get("services") or {}
    return {
        "at": metrics.get("collected_at"),
        "ac_online": power.get("ac_online"), "battery_pct": battery.get("percentage"),
        "battery_state": battery.get("state"),
        "mem_used_pct": memory.get("used_pct"),
        "disk_used_pct": max((d["used_pct"] for d in metrics.get("disks") or []), default=None),
        "load_15m": (metrics.get("system") or {}).get("load_15m"),
        "cpu_temp_c": (metrics.get("system") or {}).get("cpu_temp_c"),
        "failed_units": len(services.get("failed_system") or []) + len(services.get("failed_user") or []),
        "guard_ok": (metrics.get("guard") or {}).get("ok"),
    }


def record_history(metrics: dict[str, Any], path: Path | None = None, max_lines: int = HISTORY_MAX_LINES) -> Path:
    path = path or history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines.append(json.dumps(snapshot(metrics), ensure_ascii=False, separators=(",", ":")))
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines[-max_lines:]) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def last_sample_age_h(path: Path | None = None, now: datetime | None = None) -> float | None:
    """Hours since the last recorded sample of `health collect --record` (None: nothing readable)."""
    path = path or history_path()
    try:
        lines = [x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        at = datetime.fromisoformat(json.loads(lines[-1])["at"])
    except (OSError, IndexError, KeyError, TypeError, ValueError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - at).total_seconds() / 3600


def probe_history(path: Path | None = None) -> dict[str, Any]:
    """Digest of the recorded samples, for the /sante analysis (None when nothing was recorded yet)."""
    path = path or history_path()
    samples = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                samples.append(json.loads(line))
            except ValueError:
                continue  # a torn line must not hide the rest
    except OSError:
        return {"samples": 0}
    if not samples:
        return {"samples": 0}

    def values(key: str) -> list[float]:
        return [x[key] for x in samples if isinstance(x.get(key), (int, float))]

    battery, memory, disk = values("battery_pct"), values("mem_used_pct"), values("disk_used_pct")
    return {
        "samples": len(samples), "since": samples[0].get("at"), "until": samples[-1].get("at"),
        "on_battery_samples": sum(1 for x in samples if x.get("ac_online") is False),
        "battery_pct_min": min(battery) if battery else None,
        "mem_used_pct_max": max(memory) if memory else None,
        "disk_used_pct_max": max(disk) if disk else None,
        "samples_with_failed_units": sum(1 for x in samples if x.get("failed_units")),
        "samples_guard_not_ok": sum(1 for x in samples if x.get("guard_ok") is False),
    }


def probe_backup() -> dict[str, Any]:
    """Age and outcome of the last backup (var/backup/state.json, no network)."""
    from ultron_hub.backup.run import default_layout, status
    from ultron_hub.common.settings import load_settings

    settings = load_settings()
    info = status(default_layout(settings.var_dir, settings.todo_db))
    last_run, last_check = info["last_run"] or {}, info["last_check"] or {}
    restore = info["last_restore_test"] or {}
    return {
        "configured": info["configured"],
        "last_success_at": info["last_success_at"], "last_success_age_h": info["last_success_age_h"],
        "last_run_status": last_run.get("status"), "last_run_error": last_run.get("error"),
        "last_check_status": last_check.get("status"), "last_check_at": last_check.get("at"),
        "last_restore_status": restore.get("status"), "last_restore_at": restore.get("at"),
        "last_restore_error": restore.get("error"),
        "last_check_error": last_check.get("error"),
    }


def probe_depot() -> dict[str, Any]:
    """Last successful push to the private GitHub remote and commits of main not on origin/main (no network)."""
    from ultron_hub.common.settings import load_settings
    from ultron_hub.depot import push

    settings = load_settings()
    return push.probe(settings.root, settings.var_dir)


PROBES: dict[str, Callable[[], Any]] = {
    "system": probe_system,
    "memory": probe_memory,
    "disks": probe_disks,
    "power": probe_power,
    "services": probe_services,
    "updates": probe_updates,
    "guard": probe_guard,
    "history": probe_history,
    "backup": probe_backup,
    "depot": probe_depot,
}


def collect(only: tuple[str, ...] | None = None) -> dict[str, Any]:
    metrics: dict[str, Any] = {"collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    errors = {}
    for name, probe in PROBES.items():
        if only and name not in only:
            continue
        try:
            metrics[name] = probe()
        except Exception as exc:  # one broken probe must not hide the others
            metrics[name] = None
            errors[name] = f"{type(exc).__name__}: {exc}"
    metrics["errors"] = errors
    return metrics
