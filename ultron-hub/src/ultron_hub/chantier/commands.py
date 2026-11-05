"""`ultron-hub chantier ...` (read only, no Claude involved).

Every command prints one JSON object on stdout:
    {"status": "ok"|"error", "command": "chantier.<name>", "summary": str, "data": obj|null, "error": str|null}
"""

import argparse
from typing import Any

from ultron_hub.chantier import core
from ultron_hub.common.settings import Settings
from ultron_hub.esprit import store as journal


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None,
             error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": f"chantier.{command}", "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    chantier = sub.add_parser("chantier", help="read-only state of the chantiers (squad subjects)")
    cmds = chantier.add_subparsers(dest="chantier_command", required=True, parser_class=parser_class)
    for name, text in (("tableau", "every chantier: group, squad, what waits for the user"),
                       ("fiche", "one chantier in detail ('-' = Hors chantier)"),
                       ("conseil", "which session to launch now (breakdown, usage, tooling), plain rules"),
                       ("atoi", "everything that waits for the user, one line each with its chantier"),
                       ("archiver", "move a finished chantier to termines/ (refused while a todo is open)"),
                       ("rouvrir", "bring an archived chantier back from termines/")):
        p = cmds.add_parser(name, help=text)
        if name in ("fiche", "archiver", "rouvrir"):
            p.add_argument("sujet")
        if name == "atoi":
            p.add_argument("--chantier", default=None, help="only this chantier ('-' = Hors chantier)")
        p.add_argument("--dossier", default=None, help="prompts folder (default ~/notes/prompt; tests)")
        p.add_argument("--boites-dir", default=None, help="mailboxes folder (default ~/.local/state/ultron-hub/boites)")


    nuit = cmds.add_parser("nuit", help="chantier de nuit (todo #478): creer | fin | attendre")
    nsub = nuit.add_subparsers(dest="nuit_command", required=True, parser_class=parser_class)
    p = nsub.add_parser("creer", help="create nuit-JJ-JJ-MM (prompt, fiche, Reprise)")
    p.add_argument("--date", default=None, help="first day AAAA-MM-JJ (default today)")
    p = nsub.add_parser("fin", help="end a night chantier: keep its Reprise, delete the rest")
    p.add_argument("nom")
    p = nsub.add_parser("attendre", help="wait until the other ultron sessions are idle")
    p.add_argument("--session", required=True, help="the name of the calling session")
    p.add_argument("--max-s", type=float, default=14400)
    p.add_argument("--pause-s", type=float, default=60)
    for p in nsub.choices.values():
        p.add_argument("--dossier", default=None, help="prompts folder (default ~/notes/prompt; tests)")
        p.add_argument("--boites-dir", default=None, help=argparse.SUPPRESS)


def handle(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    cmd = args.chantier_command
    if cmd == "nuit":
        return _handle_nuit(args, settings)
    src = core.sources(args.dossier, args.boites_dir)
    if cmd == "tableau":
        data = core.tableau(settings, src)
        groups = [c["groupe"] for c in data["chantiers"]]
        return envelope(cmd, summary=f"{len(groups)} chantier(s) : 🟢 {groups.count('🟢')} · 📥 {groups.count('📥')}"
                                     f" · 💤 {groups.count('💤')}", data=data)
    if cmd == "fiche":
        data = core.fiche(settings, src, args.sujet)
        return envelope(cmd, summary=f"{data['sujet']} : {data['groupe']} {len(data['a_toi'])} à toi", data=data)
    if cmd == "conseil":
        from ultron_hub.chantier import conseil
        data = conseil.conseiller(settings, src)
        return envelope(cmd, summary=f"session conseillée : {data['chantier'] or 'Main'} ({data['raison']})", data=data)
    if cmd == "atoi":
        data = core.atoi(settings, src, args.chantier)
        return envelope(cmd, summary=f"{data['total']} élément(s) attendent l'utilisateur", data=data)
    if cmd in ("archiver", "rouvrir"):
        data = archive(settings, src, args.sujet, reopen=cmd == "rouvrir")
        return envelope(cmd, summary=f"{args.sujet} : {'archivé dans termines/' if cmd == 'archiver' else 'rouvert'}",
                        data=data)
    return envelope(cmd, status="error", error=f"unknown command: {cmd}")


def archive(settings: Settings, src: core.Sources, sujet: str, reopen: bool = False) -> dict[str, Any]:
    """Archive (or reopen) a chantier, journal it and close (or create) its group topic; also used by 🧹 Ménage proposé."""
    data = core.rouvrir(src, sujet) if reopen else core.archiver(settings, src, sujet)
    journal.record(settings, "ultron-hub", "chantier", f"Chantier {'rouvert' if reopen else 'archivé'} : {sujet}")
    try:  # topic of the chantier in the Telegram group (todo #203): never breaks the command
        from ultron_hub.groupe import chantiers
        if reopen:
            data["groupe"] = chantiers.creer(settings, sujet, chantiers.domaine_de(sujet, src.prompts))
        else:
            data["groupe"] = chantiers.fermer(settings, sujet)
    except Exception as exc:  # noqa: BLE001
        data["groupe"] = {"status": "error", "summary": f"sujet de chantier : {type(exc).__name__}: {exc}"[:300]}
    return data


def _handle_nuit(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    from datetime import date
    from ultron_hub.chantier import nuit
    from ultron_hub.common.errors import InvalidRequestError
    sub = f"nuit.{args.nuit_command}"
    src = core.sources(args.dossier, args.boites_dir)
    if args.nuit_command == "creer":
        try:
            day = date.fromisoformat(args.date) if args.date else None
        except ValueError:
            raise InvalidRequestError(f"date refusée : {args.date!r} (AAAA-MM-JJ)") from None
        data = nuit.creer(settings, src, day)
        return envelope(sub, summary=f"chantier de nuit créé : {data['nom']}", data=data)
    if args.nuit_command == "fin":
        data = nuit.fin(settings, src, args.nom)
        return envelope(sub, summary=f"{args.nom} terminé : {len(data['fait'])} action(s)", data=data)
    data = nuit.attendre(args.session, args.max_s, args.pause_s)
    return envelope(sub, summary=f"attendu {data['attendu_s']} s", data=data)
