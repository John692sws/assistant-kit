"""`ultron-hub dashboard serve [--port 8787]` (todos #130, #131): the phone page, in the foreground.
`ultron-hub dashboard cert [--redemarrer]` (todo #239): get or renew the HTTPS certificate (`tailscale cert`).
`ultron-hub dashboard suivi <job>` (todo #267): steps and final answer of a background action (merge), read-only."""

import argparse
import subprocess
from typing import Any, Callable

from ultron_hub.common.settings import load_settings

DEFAULT_PORT = 8787
PREVIEW_PORT = 8788
EX_CONFIG = 78  # configuration missing: the service is not restarted in a loop
SERVICE = "ultron-hub-dashboard.service"
SYSTEMCTL = "/usr/bin/systemctl"


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    dash = sub.add_parser("dashboard", help="phone dashboard, Tailscale only, confirmed actions (ultron-hub-dashboard.service)")
    cmds = dash.add_subparsers(dest="dashboard_command", required=True, parser_class=parser_class)
    serve = cmds.add_parser("serve", help="serve the page on the Tailscale IP until SIGTERM")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--sans-config-code-0", action="store_true",
                       help="missing config: exit 0 instead of 78 (launchd cannot exclude a status from its restarts)")
    cert = cmds.add_parser("cert", help="get or renew the HTTPS certificate (tailscale cert), say if it changed")
    cert.add_argument("--redemarrer", action="store_true", help=f"restart {SERVICE} only if the certificate changed")
    apercu = cmds.add_parser("apercu", help="this worktree's page on 127.0.0.1 with FAKE data (for agents; no device check, actions off)")
    apercu.add_argument("--port", type=int, default=PREVIEW_PORT)
    suivi = cmds.add_parser("suivi", help="state of a background action of the dashboard (merge), read-only")
    suivi.add_argument("job")
    caps = cmds.add_parser("captures", help="375x812 screenshots (dark only, todo #288) of the preview, via the browser venv (installer.sh)")
    caps.add_argument("--sortie", default=None, help="output folder (default var/captures/<date>)")
    caps.add_argument("--telegram", action="store_true", help="send the screenshots with `send` (MEDIA:<path>)")


def restart_dashboard() -> bool:
    from ultron_hub.common import plateforme
    if plateforme.est_mac():  # todo #390: launchd agent (kickstart -k)
        return plateforme.redemarrer("dashboard")["ok"]
    proc = subprocess.run([SYSTEMCTL, "--user", "restart", "--no-block", SERVICE], capture_output=True, text=True,
                          timeout=30, check=False, stdin=subprocess.DEVNULL)
    return proc.returncode == 0


def cert(redemarrer: bool, restart: Callable[[], bool] = restart_dashboard) -> dict[str, Any]:
    from ultron_hub.common.logging import get_logger
    from ultron_hub.dashboard import auth, tls

    log = get_logger("dashboard")
    try:
        out = tls.fetch(tls.cert_domain())
    except (tls.CertError, auth.DashboardConfigError, ValueError) as exc:
        log.warning("dashboard cert failed: %s", exc)
        return {"status": "error", "command": "dashboard.cert", "summary": "certificat non obtenu", "data": None,
                "error": str(exc), "exit_code": 1}
    out["restarted"] = bool(redemarrer and out["changed"] and restart())
    if redemarrer and out["changed"] and not out["restarted"]:
        log.warning("dashboard cert changed but %s restart failed", SERVICE)
        return {"status": "warn", "command": "dashboard.cert", "data": out, "error": f"redémarrage de {SERVICE} refusé",
                "summary": "certificat renouvelé, dashboard PAS redémarré", "exit_code": 1}
    summary = "certificat " + ("renouvelé" if out["changed"] else "inchangé")
    if out["restarted"]:
        summary += ", dashboard redémarré"
    log.info("dashboard cert domain=%s changed=%s restarted=%s", out["domain"], out["changed"], out["restarted"])
    return {"status": "ok", "command": "dashboard.cert", "summary": summary, "data": out, "error": None, "exit_code": 0}


def _envelope(command: str, summary: str, data: Any = None, error: str | None = None, code: int = 0) -> dict[str, Any]:
    return {"status": "error" if error else "ok", "command": command, "summary": summary, "data": data, "error": error,
            "exit_code": code}


def _apercu(args: argparse.Namespace) -> dict[str, Any]:
    import sys

    from ultron_hub.dashboard import apercu

    print(f"aperçu (FAUSSES données) sur http://127.0.0.1:{args.port}/ : Ctrl-C ou SIGTERM pour arrêter", file=sys.stderr, flush=True)
    try:
        apercu.serve(args.port)
    except KeyboardInterrupt:
        pass
    return _envelope("dashboard.apercu", "preview stopped")


def _captures(args: argparse.Namespace) -> dict[str, Any]:
    import time
    from pathlib import Path

    from ultron_hub import envoi
    from ultron_hub.dashboard import captures

    settings = load_settings()
    sortie = Path(args.sortie) if args.sortie else settings.root / "var" / "captures" / time.strftime("%Y%m%d-%H%M%S")
    try:
        data = captures.capture(settings.root, sortie)
    except captures.CapturesError as exc:
        return _envelope("dashboard.captures", "captures failed", error=str(exc), code=1)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _envelope("dashboard.captures", "captures failed", error=type(exc).__name__, code=1)
    data["sortie"] = str(sortie)
    if args.telegram:
        sent = [envoi.envoyer(f"MEDIA:{f}", target="telegram", settings=settings).ok for f in data["fichiers"]]
        data["telegram"] = {"envoyes": sum(sent), "sur": len(sent)}
        if not all(sent):
            return _envelope("dashboard.captures", "captures taken, sending failed", data, "envoi Telegram incomplet", 1)
    return _envelope("dashboard.captures", f"{len(data['fichiers'])} capture(s)", data)


def _suivi(args: argparse.Namespace) -> dict[str, Any]:
    from ultron_hub.dashboard import fond

    data = fond.suivi(load_settings(), args.job)
    if data is None:
        return _envelope("dashboard.suivi", "suivi inconnu", error="suivi inconnu ou illisible", code=1)
    return _envelope("dashboard.suivi", "terminé" if data["fini"] else "en cours", data)


def handle(args: argparse.Namespace) -> dict[str, Any]:
    if args.dashboard_command == "suivi":
        return _suivi(args)
    if args.dashboard_command == "apercu":
        return _apercu(args)
    if args.dashboard_command == "captures":
        return _captures(args)
    from ultron_hub.common.logging import setup_logging
    from ultron_hub.dashboard import auth, server

    settings = load_settings()
    setup_logging(settings.logs_dir, settings.log_level, settings.log_file, settings.log_max_bytes,
                  settings.log_backup_count)
    if args.dashboard_command == "cert":
        return cert(args.redemarrer)
    try:
        server.serve(args.port, settings.root / "config" / "dashboard.yaml")
    except auth.DashboardConfigError as exc:
        return {"status": "error", "command": "dashboard.serve", "summary": "dashboard not started", "data": None,
                "error": str(exc), "exit_code": 0 if args.sans_config_code_0 else EX_CONFIG}
    except KeyboardInterrupt:
        pass
    return {"status": "ok", "command": "dashboard.serve", "summary": "dashboard stopped", "data": None, "error": None,
            "exit_code": 0}
