"""Platform layer (todo #390): user services through systemd on Linux, launchd on macOS.

Only what the kit's v1 needs: the dashboard service and the health timer. `platform.system()` picks the side;
argv are lists, never a shell. On Linux the argv are the ones the rest of the code already uses
(`systemctl --user …`); on macOS the services are LaunchAgents (`~/Library/LaunchAgents/<label>.plist`) in the
`gui/<uid>` domain (`launchctl bootstrap / bootout / kickstart / print`). No linger on macOS: a sleeping Mac runs
nothing, the health agent's StartCalendarInterval is caught up once at wake.

`python -m ultron_hub.common.plateforme {systeme|etat|demarrer|arreter|redemarrer|installer} [service] [--gabarits D]`
prints one JSON object; exit code 0 when the operation succeeded.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

LINUX = "Linux"
MAC = "Darwin"
TIMEOUT_S = 30

# service -> (systemd unit to start/query, systemd files to install, launchd label)
SERVICES: dict[str, tuple[str, tuple[str, ...], str]] = {
    "dashboard": ("ultron-hub-dashboard.service", ("ultron-hub-dashboard.service",), "ultron-hub.dashboard"),
    "sante": ("ultron-hub-health.timer", ("ultron-hub-health.service", "ultron-hub-health.timer"), "ultron-hub.health"),
}

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def systeme() -> str:
    return platform.system()


def est_mac() -> bool:
    return systeme() == MAC


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_S, check=False, stdin=subprocess.DEVNULL)


def _service(nom: str) -> tuple[str, tuple[str, ...], str]:
    if nom not in SERVICES:
        raise ValueError(f"service inconnu : {nom!r} (connus : {', '.join(SERVICES)})")
    return SERVICES[nom]


def unite(nom: str) -> str:
    return _service(nom)[0]


def label(nom: str) -> str:
    return _service(nom)[2]


def domaine() -> str:
    return f"gui/{os.getuid()}"


def cible(nom: str) -> str:
    """launchd service target: gui/<uid>/<label>."""
    return f"{domaine()}/{label(nom)}"


def dossier_unites() -> Path:
    return Path.home() / ("Library/LaunchAgents" if est_mac() else ".config/systemd/user")


def plist(nom: str) -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{label(nom)}.plist"


# -- argv ----------------------------------------------------------------------------------------------------------

def argv_etat(nom: str) -> list[str]:
    return ["launchctl", "print", cible(nom)] if est_mac() else ["systemctl", "--user", "is-active", unite(nom)]


def argv_arreter(nom: str) -> list[str]:
    return ["launchctl", "bootout", cible(nom)] if est_mac() else ["systemctl", "--user", "stop", unite(nom)]


def argv_redemarrer(nom: str) -> list[str]:
    return ["launchctl", "kickstart", "-k", cible(nom)] if est_mac() else ["systemctl", "--user", "restart", unite(nom)]


def argv_charger(nom: str) -> list[str]:
    """macOS only: load the agent (RunAtLoad starts it)."""
    return ["launchctl", "bootstrap", domaine(), str(plist(nom))]


# -- operations ----------------------------------------------------------------------------------------------------

def _mac_etat(texte: str) -> str:
    """State from `launchctl print`: active (running), failed (last exit code not 0), waiting (loaded, idle)."""
    state = re.search(r"^\s*state = (.+)$", texte, re.M)
    if state and state.group(1).strip() == "running":
        return "active"
    code = re.search(r"^\s*last exit code = (-?\d+)", texte, re.M)
    if code and int(code.group(1)) != 0:
        return "failed"
    return "waiting"


def charge(nom: str, run: Runner = _run) -> bool:
    """macOS: whether the agent is loaded in gui/<uid>."""
    return run(argv_etat(nom)).returncode == 0


def etat(nom: str, run: Runner = _run) -> str:
    """Linux: the word `systemctl --user is-active` prints. macOS: active / waiting / failed, inactive = not loaded."""
    proc = run(argv_etat(nom))
    if est_mac():
        return _mac_etat(proc.stdout) if proc.returncode == 0 else "inactive"
    return proc.stdout.strip() or "unknown"


def demarrer(nom: str, run: Runner = _run) -> dict[str, Any]:
    if not est_mac():
        argv = ["systemctl", "--user", "start", unite(nom)]
    elif charge(nom, run):
        argv = ["launchctl", "kickstart", cible(nom)]
    else:
        argv = argv_charger(nom)
    return _resultat(argv, run(argv))


def arreter(nom: str, run: Runner = _run) -> dict[str, Any]:
    if est_mac() and not charge(nom, run):
        return {"ok": True, "argv": None, "detail": "déjà arrêté"}
    argv = argv_arreter(nom)
    return _resultat(argv, run(argv))


def redemarrer(nom: str, run: Runner = _run) -> dict[str, Any]:
    if est_mac() and not charge(nom, run):
        return demarrer(nom, run)
    argv = argv_redemarrer(nom)
    return _resultat(argv, run(argv))


def installer(nom: str, gabarits: Path, run: Runner = _run) -> dict[str, Any]:
    """Copy the filled template(s) from `gabarits` (systemd/ or launchd/) if absent (never overwrites), then enable
    and start. Rerunnable."""
    unit, files, lab = _service(nom)
    if est_mac():
        sources = [(gabarits / "launchd" / f"{lab}.plist", plist(nom))]
    else:
        dest = dossier_unites()
        sources = [(gabarits / "systemd" / f, dest / f) for f in files]
    missing = [str(s) for s, _ in sources if not s.is_file()]
    if missing:
        return {"ok": False, "argv": None, "copies": [], "detail": f"gabarit introuvable : {', '.join(missing)}"}
    copies = []
    for src, dst in sources:
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            copies.append(str(dst))
    if est_mac():
        if charge(nom, run):
            return {"ok": True, "argv": None, "copies": copies, "detail": "déjà chargé"}
        argv = argv_charger(nom)
        return {**_resultat(argv, run(argv)), "copies": copies}
    reload_ = run(["systemctl", "--user", "daemon-reload"])
    if reload_.returncode != 0:
        return {**_resultat(["systemctl", "--user", "daemon-reload"], reload_), "copies": copies}
    argv = ["systemctl", "--user", "enable", "--now", unit]
    return {**_resultat(argv, run(argv)), "copies": copies}


def _resultat(argv: list[str], proc: subprocess.CompletedProcess) -> dict[str, Any]:
    detail = (proc.stderr or proc.stdout or "").strip()[-300:]
    return {"ok": proc.returncode == 0, "argv": argv, "detail": detail}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ultron_hub.common.plateforme", description=__doc__.splitlines()[0])
    parser.add_argument("operation", choices=("systeme", "etat", "demarrer", "arreter", "redemarrer", "installer"))
    parser.add_argument("service", nargs="?", choices=tuple(SERVICES))
    parser.add_argument("--gabarits", type=Path, help="installer : dossier des gabarits remplis ($R)")
    args = parser.parse_args(argv)
    if args.operation == "systeme":
        out: dict[str, Any] = {"ok": True, "systeme": systeme(), "dossier": str(dossier_unites())}
    elif args.service is None:
        parser.error("service requis")
    elif args.operation == "etat":
        state = etat(args.service)
        out = {"ok": True, "service": args.service, "etat": state}
    elif args.operation == "installer":
        if args.gabarits is None:
            parser.error("installer : --gabarits requis")
        out = installer(args.service, args.gabarits)
    else:
        out = {"demarrer": demarrer, "arreter": arreter, "redemarrer": redemarrer}[args.operation](args.service)
    out.setdefault("systeme", systeme())
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
