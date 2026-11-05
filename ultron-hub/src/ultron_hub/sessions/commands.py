"""`ultron-hub session liste|lire|ecran` (todos #264, #283: read only) and `ecrire|choisir|interrompre` (todo #265, tmux send-keys,
see envoi.py: writing and choosing are signed `session`, interrupting is not) and `nouvelle|relancer` (todo #266, see gestion.py:
opening is signed, relaunching is not) and `fermeture|fermer` (todo #297: what closing costs, then closing, signed). No AI, no network. One JSON object on stdout:
    {"status": "ok"|"error", "command": "session.<name>", "summary": str, "data": obj|null, "error": str|null}
"""

import argparse
import sys
import time
from typing import Any

from ultron_hub.common.settings import Settings
from ultron_hub.sessions import declare, ecran, envoi, etat, gestion, journal
from ultron_hub.todo import closing


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None, error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": f"session.{command}", "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    ses = sub.add_parser("session", help="read an interactive ultron session (tmux + Claude Code journal), or write into it")
    cmds = ses.add_subparsers(dest="session_command", required=True, parser_class=parser_class)
    cmds.add_parser("liste", help="open ultron-JJ-MM-HHhMM sessions (chantier, sub-agents, working, waiting for an answer)")
    decl = cmds.add_parser("etat", help="declare where this session stands (todo #431); with no text, show the declared state")
    decl.add_argument("--session", default="", help="session name (default: the current tmux session)")
    for field in declare.FIELDS:
        decl.add_argument(f"--{field}", default=None, help=f"{field} (one line, {declare.TEXT_MAX} characters at most)")
    decl.add_argument("--efface", action="store_true", help="empty the declared state")
    one = cmds.add_parser("lire", help="last bubbles of a session and its running sub-agents")
    one.add_argument("nom", help="session name (ultron-JJ-MM-HHhMM)")
    one.add_argument("--n", type=int, default=journal.N_DEFAULT, help=f"last entries (default {journal.N_DEFAULT}, max {journal.N_MAX})")
    screen = cmds.add_parser("ecran", help="the raw tmux screen of a session (plain text) and the numbered menu it shows")
    screen.add_argument("nom", help="session name (ultron-JJ-MM-HHhMM)")
    write = cmds.add_parser("ecrire", help="type a text into the session then Enter (signed `session` token or --i-am-human)")
    write.add_argument("nom", help="session name (ultron-JJ-MM-HHhMM)")
    write.add_argument("--texte-stdin", dest="texte_stdin", action="store_true", required=True,
                       help="the text is read on stdin, never argv")
    write.add_argument("--i-am-human", dest="human", action="store_true", help="confirm at a real terminal")
    pick = cmds.add_parser("choisir", help="press one key of a choice question (signed `session` token or --i-am-human)")
    pick.add_argument("nom", help="session name (ultron-JJ-MM-HHhMM)")
    pick.add_argument("touche", choices=tuple(envoi.KEYS), help=", ".join(envoi.KEYS))
    pick.add_argument("--i-am-human", dest="human", action="store_true", help="confirm at a real terminal")
    stop = cmds.add_parser("interrompre", help="press Escape in the session (stops the current turn)")
    stop.add_argument("nom", help="session name (ultron-JJ-MM-HHhMM)")
    new = cmds.add_parser("nouvelle", help="open a new ultron session (signed `session` token or --i-am-human)")
    new.add_argument("--chantier", default="", help="chantier of the session (sheet ~/notes/prompt/<name>.md); default: Main session")
    new.add_argument("--consigne-stdin", dest="consigne_stdin", action="store_true",
                     help="the optional instruction of the day is read on stdin, never argv")
    new.add_argument("--i-am-human", dest="human", action="store_true", help="confirm at a real terminal")
    state = cmds.add_parser("fermeture", help="what closing a session would cost: sub-agents at work, uncommitted todo worktrees")
    state.add_argument("nom", help="session name (ultron-JJ-MM-HHhMM)")
    close = cmds.add_parser("fermer", help="close a session (tmux kill-session, never forced; signed `session` token or --i-am-human)")
    close.add_argument("nom", help="session name (ultron-JJ-MM-HHhMM)")
    close.add_argument("--i-am-human", dest="human", action="store_true", help="confirm at a real terminal")
    again = cmds.add_parser("relancer", help="open a new session without instruction, then close this one "
                                             "(refused while a todo worktree holds uncommitted work)")
    again.add_argument("nom", help="session name (ultron-JJ-MM-HHhMM)")


def _pilote(nom: str, entrees: list[dict[str, Any]], maj: Any) -> dict[str, Any]:
    """What the Pilote's bar and quick replies need besides the bubbles (todo #283): age, chantier, question, menu."""
    cree = next((s["cree"] for s in journal.list_sessions() if s["nom"] == nom), None)
    attend = etat.waits(entrees[-1] if entrees else None)
    return {"cree": cree, "chantier": journal.chantier_of(nom), "attend": attend, "travaille": etat.works(maj, attend),
            "menu": ecran.menu_of(nom)}


def _attend(nom: str) -> bool:
    try:
        entrees = journal.read_entries(journal.find_journal(nom), 1)["entrees"]
    except (journal.SessionError, OSError, ValueError):
        return False
    return etat.waits(entrees[-1] if entrees else None)


def handle(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    cmd = args.session_command
    try:
        if cmd == "liste":
            now = time.time()
            found = [{**s, **etat.digest(s["nom"]), "duree_s": max(0, int(now - s["cree"])),
                      "duree": gestion.duree_hmm(now - s["cree"]),
                      "declare": declare.lire(s["nom"], s["cree"], declare.dossier(settings))} for s in journal.list_sessions()]  # todo #281: home screen
            return envelope(cmd, summary=f"{len(found)} session(s)", data={"sessions": found, "chantiers": gestion.chantiers()})
        if cmd == "etat":
            nom = args.session or declare.current_session()
            values = {k: getattr(args, k) for k in declare.FIELDS}
            data = declare.ecrire(nom, declare.dossier(settings), values, efface=args.efface)
            reading = not args.efface and all(v is None for v in values.values())
            return envelope(cmd, summary=f"état de {nom} " + ("lu" if reading else "déclaré"), data=data)
        if cmd == "lire":
            data = journal.read_entries(journal.find_journal(args.nom), args.n)
            data.update(session=args.nom, **_pilote(args.nom, data["entrees"], data["maj"]))
            return envelope(cmd, summary=f"{len(data['entrees'])} entrée(s), {len(data['sous_agents'])} sous-agent(s) en cours",
                            data=data)
        if cmd == "ecran":
            data = ecran.lire(args.nom)
            data["attend"] = _attend(args.nom)
            return envelope(cmd, summary=f"{len(data['lignes'])} ligne(s), {len(data['menu'])} option(s) de menu", data=data)
        if cmd == "ecrire":
            texte = sys.stdin.read(envoi.TEXT_MAX + 2)  # one char more than allowed is enough to refuse
            if texte.endswith("\n"):  # `echo … |` adds one
                texte = texte[:-1]
            data = envoi.ecrire(args.nom, texte, settings.var_dir, human=args.human)
            return envelope(cmd, summary=f"{data['longueur']} caractères envoyés à {args.nom}", data=data)
        if cmd == "choisir":
            data = envoi.choisir(args.nom, args.touche, settings.var_dir, human=args.human)
            return envelope(cmd, summary=f"touche {args.touche} envoyée à {args.nom}", data=data)
        if cmd == "interrompre":
            data = envoi.interrompre(args.nom)
            return envelope(cmd, summary=f"Échap envoyé à {args.nom}", data=data)
        if cmd == "nouvelle":
            consigne = sys.stdin.read(gestion.CONSIGNE_MAX + 2) if args.consigne_stdin else ""
            if consigne.endswith("\n"):
                consigne = consigne[:-1]
            data = gestion.nouvelle(consigne, settings.var_dir, chantier=args.chantier, human=args.human)
            return envelope(cmd, summary=f"session {data['session']} ouverte", data=data)
        if cmd == "fermeture":
            data = gestion.fermeture(args.nom, settings)
            return envelope(cmd, summary=f"{data['sous_agents'] or 0} sous-agent(s), {len(data['travail_non_commite'])} worktree(s) à commiter",
                            data=data)
        if cmd == "fermer":
            data = gestion.fermer(args.nom, settings.var_dir, settings, human=args.human)
            return envelope(cmd, summary=f"session {args.nom} fermée (vérifié : elle a disparu)", data=data)
        if cmd == "relancer":
            data = gestion.relancer(args.nom, settings=settings)
            if data.get("mode") == "clear":
                return envelope(cmd, summary=f"session {args.nom} relancée (/clear, prompt relu)", data=data)
            return envelope(cmd, summary=f"session {data['session']} ouverte, {args.nom} fermée", data=data)
    except (journal.SessionError, closing.CloseRefused) as exc:
        return envelope(cmd, status="error", summary=str(exc), error=str(exc))
    return envelope(cmd, status="error", error=f"unknown command: {cmd}")
