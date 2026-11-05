"""`ultron-hub veille ...` subcommands (no Claude involved).

Every command prints one JSON object on stdout:
    {"status": "ok"|"error", "command": "veille.<name>", "summary": str, "data": obj|null, "error": str|null}
"""

import argparse
import sys
from pathlib import Path
from typing import Any

from ultron_hub.common.settings import Settings
from ultron_hub.esprit import store
from ultron_hub.todo import service
from ultron_hub.veille import core, propose


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None,
             error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": f"veille.{command}", "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    veille = sub.add_parser("veille", help="silent watchers: a script checks, Telegram only when something is new")
    cmds = veille.add_subparsers(dest="veille_command", required=True, parser_class=parser_class)
    add = cmds.add_parser("add", help="add a veille (the script is copied, its sha256 kept)")
    add.add_argument("nom")
    add.add_argument("--script", required=True)
    add.add_argument("--every", type=int, required=True, help="minutes between two checks (5 to 1440)")
    add.add_argument("--description", required=True, help="plain words, shown in the Telegram message")
    add.add_argument("--timeout", type=int, default=core.TIMEOUT_DEFAULT, help="seconds (5 to 300)")
    acces = cmds.add_parser("acces", help="replace what the script may read or the pages it receives")
    acces.add_argument("nom")
    acces.add_argument("--sans-lecture", action="store_true")
    acces.add_argument("--sans-url", action="store_true")
    for parser, default in ((add, []), (acces, None)):
        parser.add_argument("--lecture", action="append", default=default, help="folder put back read-only (3 max)")
        parser.add_argument("--url", action="append", default=default, help="https page given on stdin (3 max)")
    canal = cmds.add_parser("canal", help="where the news goes: telegram (default) or fil (Fil of the dashboard only)")
    canal.add_argument("nom")
    canal.add_argument("canal", choices=core.CANAUX)
    cmds.add_parser("list", help="all veilles with their state")
    run = cmds.add_parser("run", help="check the veilles that are due (the timer calls this)")
    run.add_argument("--nom", default=None)
    run.add_argument("--force", action="store_true", help="ignore the schedule")
    for name in ("pause", "resume", "remove"):
        cmds.add_parser(name).add_argument("nom")
    relancer = cmds.add_parser("relancer", help="one trial run of a paused veille (nothing sent); back in service if it works")
    relancer.add_argument("nom")
    prop = cmds.add_parser("propose", help="request + https link on stdin: Claude writes the script, tried twice in the sandbox")
    prop.add_argument("--depuis", default=None, help="proposal to make precise (stdin = the precision, once)")
    prop.add_argument("--requested-by", default=None, help="e.g. telegram:<chat_id>")
    creer = cmds.add_parser("creer", help="put a proposal in service (signed ✅ of the card, or --i-am-human)")
    creer.add_argument("id")
    creer.add_argument("--empreinte", required=True)
    creer.add_argument("--i-am-human", dest="human", action="store_true")


def handle(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    cmd = args.veille_command
    if cmd == "add":
        config = core.add(args.nom, Path(args.script), args.every, args.description, args.timeout,
                          lecture=args.lecture, urls=args.url)
        store.record(settings, "ultron-hub", "veille", f"Veille ajoutée : {args.nom} ({config['description']})")
        return envelope(cmd, summary=f"veille {args.nom} ajoutée (toutes les {args.every} min)", data=config)
    if cmd == "acces":
        lecture = [] if args.sans_lecture else args.lecture
        urls = [] if args.sans_url else args.url
        if lecture is None and urls is None:
            return envelope(cmd, status="error", error="rien à changer : --lecture, --url, --sans-lecture ou --sans-url")
        config = core.set_access(args.nom, lecture, urls)
        store.record(settings, "ultron-hub", "veille", f"Accès de la veille {args.nom} changés : "
                     f"{len(config['lecture'])} dossier(s) en lecture, {len(config['urls'])} page(s)")
        return envelope(cmd, summary=f"accès de {args.nom} changés", data=config)
    if cmd == "canal":
        config = core.set_canal(args.nom, args.canal)
        store.record(settings, "ultron-hub", "veille", f"Canal de la veille {args.nom} : {args.canal}")
        return envelope(cmd, summary=f"veille {args.nom} : canal {args.canal}", data=config)
    if cmd == "list":
        rows = core.entries()
        paused = sum(r["paused"] for r in rows)
        return envelope(cmd, summary=f"{len(rows)} veille(s), {paused} en pause", data={"veilles": rows})
    if cmd == "run":
        def send(text: str, cle_alerte: str | None = None, boutons: list | None = None, fil_seul: bool = False) -> None:
            service.send_message(text, settings.todo_notify_target, settings.hermes_binary, sujet="alertes",
                                 cle_alerte=cle_alerte, boutons=boutons or None, fil_seul=fil_seul)

        def journal(text: str) -> None:
            store.record(settings, "ultron-hub", "veille", text)

        report = core.run(send, journal, only=args.nom, force=args.force)
        if report["busy"]:
            summary = "un passage est déjà en cours"
        else:
            summary = (f"{len(report['ran'])} lancée(s), {len(report['new'])} nouveauté(s), "
                       f"{len(report['failed'])} échec(s)")
        return envelope(cmd, summary=summary, data=report)
    if cmd == "pause":
        return envelope(cmd, summary=f"veille {args.nom} en pause", data=core.pause(args.nom))
    if cmd == "resume":
        data = core.resume(args.nom)
        from ultron_hub import envoi
        envoi.marquer_reglee(core.alerte_cle(args.nom), settings)  # never raises
        return envelope(cmd, summary=f"veille {args.nom} relancée", data=data)
    if cmd == "relancer":
        try:
            data = core.relancer(args.nom)
        except core.CheckFailed as exc:  # still paused: the real cause goes back to the caller
            return envelope(cmd, status="error", summary=f"veille {args.nom} toujours en pause", error=str(exc))
        from ultron_hub import envoi
        envoi.marquer_reglee(core.alerte_cle(args.nom), settings)  # never raises
        return envelope(cmd, summary=f"veille {args.nom} relancée", data=data)
    if cmd == "remove":
        target = core.remove(args.nom)
        from ultron_hub import envoi
        envoi.oublier(core.alerte_cle(args.nom), settings)  # never raises
        store.record(settings, "ultron-hub", "veille", f"Veille retirée : {args.nom} (gardée dans la corbeille)")
        return envelope(cmd, summary=f"veille {args.nom} mise à la corbeille", data={"corbeille": str(target)})
    if cmd == "propose":
        # stdin, never argv: the request is the user's free text.
        data = propose.propose(settings, sys.stdin.read(propose.REQUEST_MAX * 4 + 1), args.requested_by, args.depuis)
        if "doublon" in data:
            return envelope(cmd, summary=f"une veille existe déjà sur ce lien : {data['doublon']['nom']}", data=data)
        return envelope(cmd, summary=f"proposition {data['id']} : {data['nom']} (empreinte {data['empreinte']})",
                        data=data)
    if cmd == "creer":
        config = propose.creer(settings, args.id, args.empreinte, human=args.human)
        store.record(settings, "ultron-hub", "veille", f"Veille créée : {config['nom']}")
        return envelope(cmd, summary=f"veille {config['nom']} en service", data=config)
    return envelope(cmd, status="error", error=f"unknown command: {cmd}")
