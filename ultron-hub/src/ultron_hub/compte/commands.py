"""`ultron-hub compte <enregistrer a|b [--remplacer] | etat | releve | budget | basculer [--vers a|b] [--i-am-human]>` (todo #263),
`ultron-hub compte connexion <demarrer --compte a|b | code --code-stdin [--compte a|b] | abandonner>` (todo #336),
`ultron-hub compte planning <voir | poser [--reserve-a N] [--reserve-b N] [--seuil N] | pause | reprendre>` (todo #534).

Prints one JSON object on stdout:
    {"status": "ok"|"error", "command": "compte.<name>", "summary": str, "data": obj|null, "error": str|null}
Never a token nor the credentials' content: only slot names, dates, quotas, an alert.
"""

import argparse
import sys
from datetime import datetime, timezone
from typing import Any

from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import Settings
from ultron_hub.compte import budget, connexion, planning, store
from ultron_hub.todo import closing

log = get_logger("compte")


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None,
             error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": f"compte.{command}", "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    compte = sub.add_parser("compte", help="two Claude accounts used in turn: register, state, switch (signed)")
    cmds = compte.add_subparsers(dest="compte_command", required=True, parser_class=parser_class)
    reg = cmds.add_parser("enregistrer", help="copy the active Claude credentials into slot a or b (0600)")
    reg.add_argument("nom", choices=store.SLOTS)
    reg.add_argument("--remplacer", action="store_true", help="overwrite a slot that holds another account")
    cmds.add_parser("etat", help="active account, quota of each registered account (read only)")
    cmds.add_parser("releve", help="append the quota of each account to the history (run every 15 min by a timer)")
    cmds.add_parser("budget", help="daily budget of each account from the history (read only, no network)")
    plan = cmds.add_parser("planning", help="daily parts of the points (auto / reserve), box of the day, pause of the auto switch")
    steps_p = plan.add_subparsers(dest="planning_command", required=True, parser_class=parser_class)
    steps_p.add_parser("voir", help="parts of each account today, threshold, pause, learning (read only, no network)")
    box = steps_p.add_parser("poser", help="set today's box until midnight (Paris); an option left out = its default")
    box.add_argument("--reserve-a", dest="reserve_a", type=int, default=None, help="reserve of A, %% of its dose (0-100)")
    box.add_argument("--reserve-b", dest="reserve_b", type=int, default=None, help="reserve of B, %% of its dose (0-100)")
    box.add_argument("--seuil", type=int, default=None, help="switch threshold, %% of the 5 h session (1-100)")
    steps_p.add_parser("pause", help="pause the automatic switch (until resumed)")
    steps_p.add_parser("reprendre", help="resume the automatic switch")
    sw = cmds.add_parser("basculer", help="switch to the other registered account (signed button or --i-am-human)")
    sw.add_argument("--vers", choices=store.SLOTS, default=None, help="expected destination (refused if it is not the other)")
    sw.add_argument("--i-am-human", dest="human", action="store_true", help="confirm at a real terminal")
    login = cmds.add_parser("connexion", help="log into a Claude account again from the dashboard (signed)")
    steps = login.add_subparsers(dest="connexion_command", required=True, parser_class=parser_class)
    start = steps.add_parser("demarrer", help="start `claude auth login` in its own tmux session and give back the link")
    start.add_argument("--compte", required=True, choices=store.SLOTS, help="slot the new login is registered into")
    start.add_argument("--i-am-human", dest="human", action="store_true", help="confirm at a real terminal")
    paste = steps.add_parser("code", help="type the code of the login page, then register the new login into its slot")
    paste.add_argument("--code-stdin", dest="code_stdin", action="store_true", required=True,
                       help="the code is read on stdin (one line), never argv")
    paste.add_argument("--compte", choices=store.SLOTS, default=None, help="expected slot (refused if another login runs)")
    paste.add_argument("--i-am-human", dest="human", action="store_true", help="confirm at a real terminal")
    steps.add_parser("abandonner", help="close the login session (nothing registered)")


def handle(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    cmd = args.compte_command
    cache_dir = settings.var_dir / "cache"
    now = datetime.now(timezone.utc)
    if cmd == "connexion":
        return _connexion(args, settings, cache_dir, now)
    try:
        if cmd == "planning":
            return _planning(args, settings, now)
        if cmd == "etat":
            data = store.etat(cache_dir, now)
            doses = {}
            for row in data["comptes"]:
                week = (row.get("quota") or {}).get("week")
                row["dose"] = budget.dose_du_jour(week.get("percent"), week.get("resets_at"), now) if isinstance(week, dict) else None
                doses[row["nom"]] = row["dose"]
            data["dose_texte"] = budget.dose_texte(doses)
            actif = (data["actif_adresse"] or data["actif"].upper()) if data["actif"] else "inconnu"
            return envelope(cmd, summary=f"compte actif : {actif}" + (" ⚠️" if data["alerte"] else ""), data=data)
        if cmd == "releve":
            data = budget.releve(settings.var_dir, cache_dir, now)
            return envelope(cmd, summary=f"relevé : {len(data['releves'])} compte(s)", data=data)
        if cmd == "budget":
            data = budget.budget(settings.var_dir, now)
            total = data["total"]
            return envelope(cmd, summary=f"budget du jour : {total['budget_jour']} points" if total else "pas encore de relevé",
                            data=data)
        if cmd == "enregistrer":
            data = store.enregistrer(args.nom, cache_dir, remplacer=args.remplacer, now=now)
            log.info("compte enregistrer slot=%s", args.nom)
            return envelope(cmd, summary=data["text"], data=data)
        if cmd == "basculer":
            def authorize(target: str, used) -> dict:
                return closing.authorize("compte", target, human=args.human, used=used,
                                         what=f"Basculer le compte Claude vers {target.upper()} ?")
            data = store.basculer(cache_dir, vers=args.vers, authorize=authorize, now=now)
            log.info("compte basculer de=%s vers=%s by=%s verifie=%s", data["de"], data["vers"],
                     data["by"].get("by"), data["verification"]["ok"])
            return envelope(cmd, summary=data["text"].splitlines()[0], data=data)
    except InvalidRequestError as exc:  # CompteError, CloseRefused: nothing changed (or the proof was spent)
        reason = str(exc) if isinstance(exc, store.CompteError) else type(exc).__name__
        log.warning("compte %s refused: %s", cmd, reason)
        return envelope(cmd, status="error", summary="refusé", error=str(exc))
    return envelope(cmd, status="error", error=f"unknown command: {cmd}")


def _planning(args: argparse.Namespace, settings: Settings, now: datetime) -> dict[str, Any]:
    """Todo #534: files only (history + var/compte/planning.json), never the network."""
    step = f"planning.{args.planning_command}"
    var_dir = settings.var_dir
    if args.planning_command == "poser":
        planning.poser(var_dir, now, args.reserve_a, args.reserve_b, args.seuil)
        log.info("compte planning poser a=%s b=%s seuil=%s", args.reserve_a, args.reserve_b, args.seuil)
    elif args.planning_command in ("pause", "reprendre"):
        planning.pause(var_dir, now, args.planning_command == "pause")
        log.info("compte planning %s", args.planning_command)
    data = planning.plan(budget.budget(var_dir, now, planning.PARIS), budget.read_history(var_dir), planning.load(var_dir), now)
    state = "en pause" if data["pause"] else f"seuil {data['seuil']} %"
    return envelope(step, summary=f"bascule auto {state}" + (f" · {data['texte']}" if data["texte"] else ""), data=data)


def _connexion(args: argparse.Namespace, settings: Settings, cache_dir, now: datetime) -> dict[str, Any]:
    """Todo #336. The link and the code never reach the log; the link goes to stdout only (the dashboard shows it)."""
    step = f"connexion.{args.connexion_command}"
    try:
        if args.connexion_command == "demarrer":
            data = connexion.demarrer(args.compte, settings.var_dir, human=args.human, now=now)
            return envelope(step, summary=f"lien de connexion au compte {args.compte.upper()} prêt", data=data)
        if args.connexion_command == "code":
            value = sys.stdin.readline(connexion.CODE_MAX + 2).strip()
            data = connexion.code(value, settings.var_dir, cache_dir, slot=args.compte, human=args.human, now=now)
            return envelope(step, summary=data["text"].splitlines()[0], data=data)
        if args.connexion_command == "abandonner":
            data = connexion.abandonner(settings.var_dir, now=now)
            return envelope(step, summary=data["text"], data=data)
    except InvalidRequestError as exc:
        # CompteError texts never carry the link or the code: the reason is logged (todo #338: it was missing)
        reason = str(exc) if isinstance(exc, store.CompteError) else type(exc).__name__
        log.warning("compte %s refused: %s", step, reason)
        return envelope(step, status="error", summary="refusé", error=str(exc))
    return envelope(step, status="error", error=f"unknown command: {args.connexion_command}")
