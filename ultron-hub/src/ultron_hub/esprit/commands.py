"""`ultron-hub esprit|journal|profil|savoir ...`: the shared mind of Ultron (todo #47) and its notebook (#63).

Every command prints one JSON object on stdout:
    {"status": "ok"|"error", "command": "<group>.<name>", "summary": str, "data": obj|null, "error": str|null}
except `esprit context --claude-hook`, which prints the JSON a Claude Code SessionStart hook expects.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.settings import Settings
from ultron_hub.esprit import contexte, erreurs, etat, focus, reprise, savoir, store, summary
from ultron_hub.todo import closing, service

GROUPS = ("esprit", "journal", "profil", "savoir")


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None,
             error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": command, "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    esprit = sub.add_parser("esprit", help="shared mind of Ultron: profile, charter, journal (todo #47)")
    cmds = esprit.add_subparsers(dest="esprit_command", required=True, parser_class=parser_class)
    init = cmds.add_parser("init", help="create the folder and the missing files (never overwrites)")
    init.add_argument("--profil-from", default=None, help="initial profile (Markdown file)")
    context = cmds.add_parser("context", help="profile, charter, the end of the journal and the notebook index")
    context.add_argument("--limit", type=int, default=store.TAIL_DEFAULT)
    context.add_argument("--claude-hook", action="store_true",
                         help="print a Claude Code SessionStart hook answer (nothing for ultron-hub jobs)")
    ctx = cmds.add_parser("contexte", help="context gauge: PostToolUse hook warning at 50%% and 60%% (todo #175)")
    ctx.add_argument("--claude-hook", action="store_true", required=True, help="stdin = Claude Code PostToolUse hook JSON")
    foc = cmds.add_parser("focus", help="focus of the week: 1 or 2 domains (todo #165, #186)")
    action = foc.add_mutually_exclusive_group()
    action.add_argument("--set", nargs="+", metavar="DOMAINE", default=None, help="new focus (1 or 2 domains: systeme, business, perso)")
    action.add_argument("--ask", action="store_true", help="the Sunday question (text; --send delivers it)")
    foc.add_argument("--send", action="store_true", help="with --ask: send it on Telegram (envoi)")
    foc.add_argument("--by", default="cli")
    cmds.add_parser("etat", help="regenerate etat.md: focus, branches, what waits for the user, merges (todo #170, no AI)")
    rep = cmds.add_parser("reprise-archiver", help="keep the 3 latest Reprise entries of the orchestrator prompt (todo #169)")
    rep.add_argument("--garder", type=int, default=reprise.KEEP)
    rep.add_argument("--essai", action="store_true", help="say what would move, write nothing")
    rep.add_argument("--chantier", default=None, help="the Reprise of a chantier session (~/notes/prompt/reprises/<chantier>.md)")
    rep.add_argument("--fichier", default=None, help=argparse.SUPPRESS)
    rep.add_argument("--archives", default=None, help=argparse.SUPPRESS)

    journal = sub.add_parser("journal", help="Ultron's shared journal")
    jcmds = journal.add_subparsers(dest="journal_command", required=True, parser_class=parser_class)
    add = jcmds.add_parser("add", help="add one entry")
    add.add_argument("texte")
    add.add_argument("--source", required=True, help="e.g. claude-code, hermes, ultron-hub")
    add.add_argument("--type", dest="kind", default="note", help="e.g. note, decision, todo, mission")
    add.add_argument("--todo", type=int, default=None)
    tail = jcmds.add_parser("tail", help="latest entries")
    tail.add_argument("--limit", type=int, default=store.TAIL_DEFAULT)
    search = jcmds.add_parser("search", help="search the journal (archives included)")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=store.SEARCH_MAX)
    summ = jcmds.add_parser("summarize", help="one-line summary of a conversation (haiku)")
    origin = summ.add_mutually_exclusive_group(required=True)
    origin.add_argument("--claude-hook", action="store_true", help="stdin = Claude Code SessionEnd hook JSON")
    origin.add_argument("--stdin-text", action="store_true", help="stdin = plain transcript (Hermes)")
    origin.add_argument("--pending", default=None, help="file of var/journal-pending/ (detached run)")
    summ.add_argument("--source", default=None, help="claude-code (default with --claude-hook) or hermes")
    summ.add_argument("--detach", action="store_true", help="return at once, summarize in the background")

    profil = sub.add_parser("profil", help="the user's common profile and the proposed facts")
    pcmds = profil.add_subparsers(dest="profil_command", required=True, parser_class=parser_class)
    pcmds.add_parser("show", help="profile and pending proposals")
    pcmds.add_parser("pending", help="pending proposals")
    propose = pcmds.add_parser("propose", help="propose a fact; the user decides with ✅ on Telegram")
    propose.add_argument("fait")
    propose.add_argument("--source", required=True, help="e.g. agent:hermes, claude-code")
    propose.add_argument("--no-notify", action="store_true", help="do not announce it on Telegram")
    accept = pcmds.add_parser("accept", help="add a proposed fact to the profile (signed ✅ or --i-am-human)")
    accept.add_argument("id", type=int)
    accept.add_argument("--text", default=None, help="corrected text (✏️)")
    accept.add_argument("--i-am-human", dest="human", action="store_true")
    reject = pcmds.add_parser("reject", help="refuse a proposed fact")
    reject.add_argument("id", type=int)

    sav = sub.add_parser("savoir", help="Ultron's knowledge notebook: fiches filed by the user's ✅ (todo #63)")
    scmds = sav.add_subparsers(dest="savoir_command", required=True, parser_class=parser_class)
    lister = scmds.add_parser("lister", help="fiches, newest first")
    lister.add_argument("--projet", default=None, choices=savoir.PROJETS)
    lister.add_argument("--limit", type=int, default=None)
    chercher = scmds.add_parser("chercher", help="fiches containing every word (case and accents ignored)")
    chercher.add_argument("mots", nargs="+")
    chercher.add_argument("--limit", type=int, default=savoir.SEARCH_MAX)
    voir = scmds.add_parser("voir", help="one fiche")
    voir.add_argument("id")
    verifier = scmds.add_parser("verifier", help="check a draft and show the fiche to file (writes nothing)")
    verifier.add_argument("brouillon")
    ranger = scmds.add_parser("ranger", help="file a draft (signed ✅ Ranger or --i-am-human)")
    ranger.add_argument("brouillon")
    ranger.add_argument("--i-am-human", dest="human", action="store_true")
    scmds.add_parser("index", help="regenerate index.md")
    err = scmds.add_parser("erreur", help="errors met and their fix: add | chercher | lister (todo #506)")
    ecmds = err.add_subparsers(dest="erreur_command", required=True, parser_class=parser_class)
    eadd = ecmds.add_parser("add", help="file an error (same symptom = one more occurrence); no ✅, these are facts")
    eadd.add_argument("--symptome", required=True, help=f"<= {erreurs.SYMPTOME_MAX} characters")
    eadd.add_argument("--cause", required=True, help=f"<= {erreurs.CAUSE_MAX} characters")
    eadd.add_argument("--remede", required=True, help=f"<= {erreurs.REMEDE_MAX} characters")
    eadd.add_argument("--projet", default=None, choices=savoir.PROJETS)
    eadd.add_argument("--todo", type=int, default=None)
    eadd.add_argument("--par", default=None)
    echerche = ecmds.add_parser("chercher", help="error fiches containing every word")
    echerche.add_argument("mots", nargs="+")
    echerche.add_argument("--limit", type=int, default=savoir.SEARCH_MAX)
    elister = ecmds.add_parser("lister", help="error fiches, newest first")
    elister.add_argument("--limit", type=int, default=None)


def command_name(args: argparse.Namespace) -> str:
    return f"{args.command}.{getattr(args, args.command + '_command')}"


def handle(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    name = command_name(args)
    if args.command == "esprit":
        return _esprit(args, settings, name)
    if args.command == "journal":
        return _journal(args, settings, name)
    if args.command == "savoir":
        return _savoir(args, settings, name)
    return _profil(args, settings, name)


def _esprit(args, settings: Settings, name: str) -> dict[str, Any]:
    if args.esprit_command == "init":
        result = store.init(settings, Path(args.profil_from).expanduser() if args.profil_from else None)
        created = ", ".join(result["created"]) or "rien"
        return envelope(name, summary=f"esprit prêt ({result['dir']}) ; créés : {created}", data=result)
    if args.esprit_command == "focus":
        return _focus(args, settings, name)
    if args.esprit_command == "etat":
        data = etat.regenerate(settings)
        return envelope(name, summary=f"état du système écrit ({data['bytes']} octets)", data=data)
    if args.esprit_command == "reprise-archiver":
        fichier, nom = reprise.of_chantier(args.chantier) if args.chantier else (reprise.current(), "reprises")
        data = reprise.archive(Path(args.fichier) if args.fichier else fichier,
                               Path(args.archives) if args.archives else reprise.ARCHIVES, args.garder, dry_run=args.essai,
                               nom=nom)
        if data["ecrit"]:
            quoi = f"du chantier {args.chantier}" if args.chantier else "du prompt orchestrateur"
            store.record(settings, "claude-code", "note",
                         f"Reprise {quoi} : {data['archivees']} entrée(s) archivée(s) dans {data['archive']}")
        return envelope(name, summary=f"{data['archivees']} entrée(s) à archiver, {data['gardees']} gardée(s)"
                        + ("" if data["ecrit"] or not data["archivees"] else " (essai, rien écrit)"), data=data)
    data = {**store.persona(settings), "journal": store.journal_context(settings, args.limit),
            "savoir": _savoir_context(settings, savoir.hermes_text), "focus": focus.read(settings)}
    return envelope(name, summary="contexte de l'esprit" if data.get("profil") is not None else "esprit absent",
                    data=data)


def _focus(args, settings: Settings, name: str) -> dict[str, Any]:
    if args.send and not args.ask:
        raise InvalidRequestError("--send va avec --ask")
    if args.set is not None:
        data = focus.write(settings, args.set, args.by)
        return envelope(name, summary=f"focus : {' + '.join(data['domaines'])}", data=data)
    current = focus.read(settings)
    if args.ask:
        text = focus.ask_text(current)
        if args.send:
            service.send_message(text, settings.todo_notify_target, settings.hermes_binary)
        return envelope(name, summary="question du focus" + (" envoyée" if args.send else ""),
                        data={"focus": current, "text": text, "sent": bool(args.send)})
    return envelope(name, summary=focus.line(current), data={"focus": current, "line": focus.line(current), "domaines": focus.domaines_info()})


def _savoir_context(settings: Settings, build) -> str:
    """Best effort: an unreadable notebook never hides the profile nor the journal."""
    try:
        return build(settings)
    except Exception:
        store.log.warning("savoir: context unreadable", exc_info=True)
        return ""


def claude_hook_contexte(settings: Settings) -> dict[str, Any]:
    """Answer of the PostToolUse hook (todo #175): the stdin JSON is read here, any error means {}."""
    try:
        return contexte.hook(settings, sys.stdin.read())
    except Exception:  # noqa: BLE001 - silence, never a blocking error
        return {}


def claude_hook_context(settings: Settings, limit: int) -> dict[str, Any]:
    """Answer of the SessionStart hook: profile, charter, journal and the notebook (todo #68)."""
    reason = summary.skip_reason(settings)
    if reason and reason != summary.MIND_ABSENT:  # an ultron-hub job: nothing at all
        return {}
    parts = []
    if not reason:
        persona = store.persona(settings)
        if persona.get("charte"):
            parts.append(persona["charte"])
        parts.append(focus.line(focus.read(settings)))
        if persona.get("profil"):
            parts.append(persona["profil"])
        journal = store.journal_context(settings, min(limit, store.HOOK_JOURNAL_MAX), skip_noise=True)
        if journal:
            parts.append(journal)
    notebook = _savoir_context(settings, savoir.hook_text)
    if notebook:
        parts.append(notebook)
    if not parts:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n\n".join(parts)}}


def _journal(args, settings: Settings, name: str) -> dict[str, Any]:
    cmd = args.journal_command
    if cmd == "add":
        entry = store.add(settings, args.source, args.kind, args.texte, args.todo)
        return envelope(name, summary=store.format_entry(entry), data=entry)
    if cmd == "tail":
        rows = store.tail(settings, args.limit)
        return envelope(name, summary=f"{len(rows)} entrée(s)",
                        data={"entries": rows, "text": "\n".join(map(store.format_entry, rows)) or "journal vide"})
    if cmd == "search":
        rows = store.search(settings, args.query, args.limit)
        return envelope(name, summary=f"{len(rows)} résultat(s)",
                        data={"entries": rows, "text": "\n".join(map(store.format_entry, rows)) or "aucun résultat"})
    return _summarize(args, settings, name)


def _summarize(args, settings: Settings, name: str) -> dict[str, Any]:
    if args.claude_hook:
        source = args.source or "claude-code"
        text, reason = summary.claude_hook_input(settings, sys.stdin.read())
        if text is None:
            return envelope(name, summary=f"ignoré : {reason}", data={"recorded": False, "reason": reason})
    elif args.stdin_text:
        source = args.source or "hermes"
        reason = summary.skip_reason(settings)
        if reason:
            return envelope(name, summary=f"ignoré : {reason}", data={"recorded": False, "reason": reason})
        text = sys.stdin.read()
    else:
        source = args.source or "claude-code"
        text = summary.read_pending(settings, args.pending)
    if args.detach and not args.pending:
        result = summary.detach(settings, text, source)
    else:
        result = summary.summarize(settings, text, source)
    if result.get("recorded"):
        label = store.format_entry(result["entry"])
    else:
        label = "résumé lancé en arrière-plan" if result.get("detached") else f"rien à noter : {result['reason']}"
    return envelope(name, summary=label, data=result)


def _profil(args, settings: Settings, name: str) -> dict[str, Any]:
    cmd = args.profil_command
    if cmd in ("show", "pending"):
        pending = store.proposals(settings)
        data = {"pending": pending}
        if cmd == "show":
            data["profil"] = store.read_doc(settings, store.PROFIL)
        return envelope(name, summary=f"{len(pending)} proposition(s) en attente", data=data)
    if cmd == "propose":
        row = propose_fact(settings, args.fait, args.source, notify=not args.no_notify)
        return envelope(name, summary=f"proposition {row['id']} en attente de ton ✅", data=row)
    if cmd == "accept":
        by = closing.authorize("profil", args.id, human=args.human, what=f"Ajouter au profil la proposition {args.id} ?",
                               used=lambda proof: store.proof_used(settings, proof))
        row = store.decide(settings, args.id, True, by, args.text)
        return envelope(name, summary=f"ajouté au profil : {row['texte_final']}", data=row)
    if cmd == "reject":
        row = store.decide(settings, args.id, False, {"by": "cli"})
        return envelope(name, summary=f"proposition {args.id} refusée", data=row)
    raise AssertionError(f"unhandled profil command: {cmd}")


def _savoir(args, settings: Settings, name: str) -> dict[str, Any]:
    cmd = args.savoir_command
    if cmd == "lister":
        rows = savoir.entries(settings, args.projet, args.limit)
        return envelope(name, summary=f"{len(rows)} fiche(s)" if rows else "carnet vide (aucune fiche rangée)",
                        data={"fiches": rows, "text": "\n".join(map(savoir.format_line, rows)) or "aucune fiche"})
    if cmd == "chercher":
        rows = savoir.search(settings, " ".join(args.mots), args.limit)
        return envelope(name, summary=f"{len(rows)} fiche(s) trouvée(s)",
                        data={"fiches": rows, "text": "\n".join(map(savoir.format_line, rows)) or "aucun résultat"})
    if cmd == "voir":
        row = savoir.show(settings, args.id)
        return envelope(name, summary=f"{row['titre']} ({row['projet']}, {row['date']})", data=row)
    if cmd == "verifier":
        fiche = savoir.check_draft(Path(args.brouillon), settings)
        removed = f", {fiche['removed']} phrase(s) retirée(s)" if fiche["removed"] else ""
        return envelope(name, summary=f"brouillon valide : {fiche['fields']['titre']}{removed}", data=fiche)
    if cmd == "ranger":
        draft = Path(args.brouillon)
        fiche = savoir.check_draft(draft, settings)  # refuse a bad draft before asking for any proof
        by = closing.authorize("savoir", fiche["empreinte"], human=args.human,
                               what=f"Ranger la fiche « {fiche['fields']['titre']} » ({fiche['fields']['projet']}) ?",
                               used=lambda proof: savoir.proof_used(settings, proof))
        row = savoir.rank(settings, draft, by, expected=fiche["empreinte"])
        return envelope(name, summary=f"fiche rangée : {row['fields']['titre']} ({row['id']})", data=row)
    if cmd == "erreur":
        return _erreur(args, settings, name)
    if cmd == "index":
        result = savoir.rebuild_index(settings)
        label = f"index régénéré ({result['count']} fiche(s))" if result["written"] else "carnet vide : rien à indexer"
        return envelope(name, summary=label, data=result)
    raise AssertionError(f"unhandled savoir command: {cmd}")


def _erreur(args, settings: Settings, name: str) -> dict[str, Any]:
    cmd = args.erreur_command
    if cmd == "add":
        row = erreurs.add(settings, args.symptome, args.cause, args.remede, args.projet, args.todo, args.par)
        label = "erreur notée" if row["nouvelle"] else f"erreur déjà connue : occurrence {row['occurrences']}"
        return envelope(name, summary=f"{label} ({row['id']})", data=row)
    if cmd == "chercher":
        rows = erreurs.search(settings, " ".join(args.mots), args.limit)
    else:
        rows = erreurs.entries(settings, args.limit)
    text = "\n".join(map(savoir.format_line, rows)) or ("aucun résultat" if cmd == "chercher" else "aucune erreur notée")
    return envelope(name, summary=f"{len(rows)} erreur(s)", data={"fiches": rows, "text": text})


def propose_fact(settings: Settings, fact: str, source: str, notify: bool = True) -> dict:
    """The one way to propose a fact (`profil propose`, mission review): saved pending, journaled, announced."""
    row = store.propose(settings, fact, source)
    if not row.get("doublon"):
        store.record(settings, source, "profil", f"Fait proposé (proposition {row['id']}) : {row['texte']}")
        if notify:
            _notify(settings, row)
    return row


def _notify(settings: Settings, row: dict) -> None:
    """Best effort: the proposal is saved even when Telegram is unreachable (/profil lists it)."""
    text = (f"💡 Proposition pour ton profil (#{row['id']}, par {row['source']}) :\n{row['texte']}\n\n"
            "Pour décider : /profil")
    try:
        service.send_message(text, settings.todo_notify_target, settings.hermes_binary)
    except Exception:
        store.log.warning("profil: proposal %s not announced", row["id"], exc_info=True)

