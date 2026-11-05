"""Who is calling: `tailscale whois` against a closed list of devices (config/dashboard.yaml).

Fail closed: any doubt (no answer, unknown device, the server itself) = refused. Nothing here reads a secret.
"""

import ipaddress
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

TAILSCALE = "tailscale"
TIMEOUT_S = 5
CGNAT = ipaddress.ip_network("100.64.0.0/10")
HOST_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,252}")


@dataclass(frozen=True)
class Device:
    name: str
    node_id: str


class DashboardConfigError(Exception):
    pass


def _run_json(*argv: str) -> dict[str, Any]:
    proc = subprocess.run([TAILSCALE, *argv], capture_output=True, text=True, timeout=TIMEOUT_S, check=False,
                          stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise DashboardConfigError(f"tailscale {argv[0]} failed")
    data = json.loads(proc.stdout)
    if not isinstance(data, dict):
        raise DashboardConfigError(f"tailscale {argv[0]}: unexpected output")
    return data


def tailscale_ip() -> str:
    """The server's own Tailscale IPv4 (never 0.0.0.0 or 127.0.0.1)."""
    proc = subprocess.run([TAILSCALE, "ip", "-4"], capture_output=True, text=True, timeout=TIMEOUT_S, check=False,
                          stdin=subprocess.DEVNULL)
    lines = proc.stdout.split()
    try:
        ip = ipaddress.ip_address(lines[0])
    except (IndexError, ValueError):
        raise DashboardConfigError("no Tailscale IPv4 address") from None
    if proc.returncode != 0 or ip not in CGNAT:
        raise DashboardConfigError("no Tailscale IPv4 address")
    return str(ip)


def self_node_id() -> str:
    return self_status()[0]


def self_status() -> tuple[str, tuple[str, ...]]:
    """(the server's node ID, its Tailscale names: MagicDNS name and short host name), for the Host check."""
    me = _run_json("status", "--json").get("Self") or {}
    names = [str(me.get("DNSName") or "").rstrip(".").lower(), str(me.get("HostName") or "").lower()]
    return str(me.get("ID") or ""), tuple(n for n in dict.fromkeys(names) if n and HOST_RE.fullmatch(n))


def whois_node_id(ip: str) -> str:
    """Stable node ID of the machine behind `ip` ('' when unknown)."""
    try:
        ipaddress.ip_address(ip)
        node = _run_json("whois", "--json", ip).get("Node") or {}
    except (ValueError, DashboardConfigError, subprocess.TimeoutExpired, OSError):
        return ""
    return str(node.get("StableID") or "")


def load_devices(path: Path) -> tuple[Device, ...]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        rows = raw["appareils"]
        devices = tuple(Device(str(r["nom"]), str(r["id"])) for r in rows)
    except (OSError, yaml.YAMLError, KeyError, TypeError) as exc:
        raise DashboardConfigError(f"{path.name} illisible : {type(exc).__name__}") from None
    if not devices or not all(d.name and d.node_id for d in devices):
        raise DashboardConfigError(f"{path.name} : liste d'appareils vide ou incomplète")
    return devices


class Gate:
    """`caller(ip)` -> the allowed device name, or None. The server's own node is refused even if listed."""

    def __init__(self, devices: tuple[Device, ...], self_id: str, server_ip: str,
                 whois: Callable[[str], str] = whois_node_id):
        self.devices = {d.node_id: d.name for d in devices if d.node_id != self_id}
        self.self_id = self_id
        self.server_ip = server_ip
        self.whois = whois

    def caller(self, ip: str) -> str | None:
        if ip == self.server_ip:
            return None
        node_id = self.whois(ip)
        if not node_id or node_id == self.self_id:
            return None
        return self.devices.get(node_id)
