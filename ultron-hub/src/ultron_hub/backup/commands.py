"""`ultron-hub backup run|status|check` and `ultron-hub heartbeat` (no Claude involved).

Every command prints one JSON object on stdout:
    {"status": "ok"|"error", "command": "backup.<name>"|"heartbeat", "summary": str, "data": obj|null,
     "error": str|null}
"""

import argparse
from typing import Any

from ultron_hub.backup import heartbeat, run as backup
from ultron_hub.common.settings import Settings, load_settings


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None,
             error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": command, "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    cmd = sub.add_parser("backup", help="encrypted backup to Google Drive (restic + rclone)")
    cmds = cmd.add_subparsers(dest="backup_command", required=True, parser_class=parser_class)
    cmds.add_parser("run", help="stage databases, back up, apply retention, ping healthchecks.io")
    cmds.add_parser("status", help="last backup and check, from var/backup/state.json (no network)")
    cmds.add_parser("restore-test", help="restore the sentinel + one database of the last snapshot into a "
                                         "throwaway folder and verify them (monthly)")
    cmds.add_parser("check", help="restic check on a 5 %% data subset")
    sub.add_parser("heartbeat", help="ping healthchecks.io: server alive, internal watcher collecting")


def handle_heartbeat(args: argparse.Namespace) -> dict[str, Any]:
    try:  # todo #501: reprise des objectifs coupés, une fois par passage, sans jamais casser le heartbeat
        from ultron_hub import reprise
        reprise.reprise_auto(load_settings())
    except BaseException:  # noqa: BLE001
        pass
    try:  # todo #509: routines échues, un lancement au plus par passage, jamais bloquant
        from ultron_hub import routines_lanceur
        routines_lanceur.lanceur_auto(load_settings())
    except BaseException:  # noqa: BLE001
        pass
    try:  # todo #534: automatic account switch on a near quota (written rule), never blocking
        from ultron_hub.compte import auto
        auto.heartbeat(load_settings())
    except BaseException:  # noqa: BLE001
        pass
    result = heartbeat.beat()
    if not result["configured"]:
        return envelope("heartbeat", summary="non configuré (pas de HC_VIVANT_URL)", data=result)
    if result["pinged"] == "ok":
        summary = "battement envoyé"
    else:
        summary = f"échec signalé : {result['pouls']}"
    if result["ping_error"]:
        summary += f" (ping non reçu : {result['ping_error']})"
    return envelope("heartbeat", summary=summary, data=result)


def _age(hours: float | None) -> str:
    if hours is None:
        return "jamais"
    return f"il y a {hours:.0f} h" if hours >= 1 else "il y a moins d'une heure"


def handle_backup(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    cmd = args.backup_command
    live = backup.main_tree(settings.root)
    if live != settings.root:
        settings = load_settings(live)
    layout = backup.default_layout(settings.var_dir, settings.todo_db)
    name = f"backup.{cmd}"
    if cmd == "status":
        info = backup.status(layout)
        summary = f"dernière sauvegarde réussie : {_age(info['last_success_age_h'])}"
        if not info["configured"]:
            summary += " (non configurée)"
        if info.get("warning"):
            summary += f" ; ATTENTION : {info['warning']}"
        return envelope(name, summary=summary, data=info)
    if cmd == "run":
        result = backup.run_backup(layout)
    elif cmd == "restore-test":
        result = backup.run_restore_test(layout)
        if result["status"] == "ok":
            return envelope(name, summary=f"restauration vérifiée (snapshot {result['snapshot_id']})", data=result)
    elif cmd == "check":
        result = backup.run_check(layout)
    else:
        return envelope(name, status="error", error=f"unknown command: {cmd}")
    if result["status"] == "error":
        return envelope(name, status="error", summary="échec", data=result, error=result["error"])
    summary = {"ok": "réussie", "partial": "réussie avec avertissements"}[result["status"]]
    return envelope(name, summary=summary, data=result)
