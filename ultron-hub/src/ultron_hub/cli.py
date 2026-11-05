"""Command line entry point.

    ultron-hub run <action> [--params JSON] [--job-id ID] [--requested-by WHO] [--json]
    ultron-hub todo <add|list|show|done|reopen|cancel|edit|digest|meta|board|schema-retour> ...
    ultron-hub esprit <init|context|contexte|focus|etat|reprise-archiver> | journal <add|tail|search|summarize> | profil <show|pending|propose|accept|reject>
    ultron-hub savoir <lister|chercher|voir|verifier|ranger|index> ...
    ultron-hub health <collect|battery>
    ultron-hub priv <status|preview|user-preview>
    ultron-hub branch <info|merge|after|after-run|after-refuse> <todo_id> [n]
    ultron-hub usage
    ultron-hub telegram <on|off|etat>   (notifications Telegram : coupées par défaut)
    ultron-hub compte <enregistrer a|b [--remplacer] | etat | basculer [--vers a|b] [--i-am-human]>   (deux comptes Claude)
    ultron-hub compte connexion <demarrer --compte a|b | code --code-stdin | abandonner>   (se reconnecter, todo #336)
    ultron-hub video <collect <url> | analyse <id> | actions <id> (stdin)>
    ultron-hub montage <prepare <audio> | render <id> [--apply E] | make <audio> | fix <id> (stdin) | keep <id> |
                        retouch <id> --scene N|--style T|--duree D | purge [--days N]>
    ultron-hub backup <run|status|check>
    ultron-hub heartbeat
    ultron-hub veille <add|acces|canal|list|run|pause|resume|relancer|remove|propose|creer> ...
    ultron-hub depot <push>   (git push origin main --tags vers le GitHub privé, minuterie quotidienne, todo #454)
    ultron-hub menage <scan>
    ultron-hub cap <collect | report>
    ultron-hub rapports <dimanche | preparer | appliquer --empreinte X>   (rapports du dimanche, un seul ✅ signé, todo #417 ; preparer = minuterie du dimanche, #460)
    ultron-hub habitudes <liste | voir | installer --dossier D --empreinte E | jeter --dossier D>   (todo #429)
    ultron-hub routine <propose ... | liste | voir --nom N | activer --nom N --empreinte E | refuser --nom N | retirer --nom N | lancer [--essai]>   (todos #503, #509)
    ultron-hub objectif <add --but T [--critere C]... | show ID | list [--statut S] | etape ID --etape T | preuve ID --texte T | lier ID --session S | reprendre [--essai] | stop-hook | verifier ID [--dossier D] [--timeout S]>   (todo #496, #497)
    ultron-hub banc <noter --reussis N --sur 10 [--cout P] [--note T] [--date AAAA-MM-JJ] | historique>   (todo #513)
    ultron-hub bilan semaine [--send]   (todos, boutons, scripts contre IA, quota des 7 derniers jours, sans IA)
    ultron-hub docs <new | present <id> | actions <id> (stdin)>
    ultron-hub souvenirs <indexer | chercher <mots...>>
    ultron-hub chantier <tableau | conseil | fiche S | atoi [--chantier S] | archiver S | rouvrir S | nuit creer|fin N|attendre --session S>
    ultron-hub bot run   (bot Telegram direct, sans Hermes ; long polling au premier plan)
    ultron-hub dashboard <serve [--port 8787] | cert [--redemarrer]>   (page du téléphone, IP Tailscale seulement, HTTPS)
    ultron-hub dashboard apercu [--port 8788] | captures [--sortie DIR] [--telegram]   (aperçu sur 127.0.0.1 avec fausses données, captures 375 px)
    ultron-hub dashboard suivi <job>   (étapes et résultat d'une fusion lancée en fond par le dashboard)
    ultron-hub prompt interactif [<sujet>] [--sortie F]
    ultron-hub prompt mesure [<sujet>]
    ultron-hub escouade <prompt <sujet> --limite T [--fiche P] [--drapeau P] [--sortie F] | sujets | nouveau S --titre T>

stdout carries exactly one JSON result object; logs go to stderr and var/logs/.
Exit code: 0 if status == "ok", 1 otherwise.
"""

import argparse
import json
import os
import sys

from ultron_hub.common.errors import InvalidRequestError, UltronHubError
from ultron_hub.common.logging import get_logger, setup_logging
from ultron_hub.common.settings import load_settings
from ultron_hub.core.job import JobRequest, JobResult, new_job_id
from ultron_hub.bot import commands as bot_commands
from ultron_hub.chantier import commands as chantier_commands
from ultron_hub.escouade import commands as escouade_commands
from ultron_hub.dashboard import commands as dashboard_commands
from ultron_hub.escouade import interactif as prompt_commands
from ultron_hub.esprit import commands as esprit_commands
from ultron_hub.core.runner import run_job
from ultron_hub.backup import commands as backup_commands
from ultron_hub.health import commands as health_commands
from ultron_hub import branch_commands, priv_commands
from ultron_hub.docs import commands as docs_commands
from ultron_hub.montage import commands as montage_commands
from ultron_hub.todo import commands as todo_commands
from ultron_hub.usage import commands as usage_commands
from ultron_hub.video import commands as video_commands
from ultron_hub.groupe import commands as groupe_commands
from ultron_hub.menage import commands as menage_commands
from ultron_hub.audit import commands as audit_commands
from ultron_hub import rapports as rapports_commands
from ultron_hub import habitudes as habitudes_commands
from ultron_hub import routines as routines_commands
from ultron_hub import banc as banc_commands
from ultron_hub import objectif as objectif_commands
from ultron_hub.cap import commands as cap_commands
from ultron_hub.cout import commands as cout_commands
from ultron_hub.kit import commands as kit_commands
from ultron_hub.sessions import commands as session_commands
from ultron_hub import notifs as notifs_commands
from ultron_hub import recus as recus_commands
from ultron_hub.compte import commands as compte_commands
from ultron_hub.souvenirs import commands as souvenirs_commands
from ultron_hub.veille import commands as veille_commands
from ultron_hub.depot import commands as depot_commands


class _ArgumentParser(argparse.ArgumentParser):
    # argparse would print usage and exit(2); we need a JSON result instead.
    def error(self, message: str):
        raise InvalidRequestError(f"invalid arguments: {message}")


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="ultron-hub", description="Run ultron-hub tasks headlessly.")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_ArgumentParser)
    run = sub.add_parser("run", help="run a task from tasks/<action>/")
    run.add_argument("action")
    run.add_argument("--params", default="{}", help="JSON object of prompt parameters")
    run.add_argument("--job-id", default="", help="job id (default: timestamp + short uuid)")
    run.add_argument("--requested-by", default=None, help="e.g. telegram:<user_id>")
    run.add_argument("--json", action="store_true",
                     help="JSON output on stdout (always the case; kept for explicit calls)")
    send = sub.add_parser("send", help="send a Telegram message (the common sending point: hermes or direct, MEDIA:<path> = attachment)")
    send.add_argument("text", nargs="?", default=None, help="the message (default: read stdin)")
    send.add_argument("--chantier", default=None, help="chantier name: its own topic if it exists, else the --sujet one")
    send.add_argument("--sujet", default=None, help="topic of the group (systeme, business, perso, missions, alertes, revue-de-cap); unknown = private chat")
    send.add_argument("--genre", default=None, help="genre of the notification in the fil: feu-vert, question, alerte, fait, bilan, fichier (default: deduced from the text)")
    send.add_argument("-t", "--to", default="telegram", help="telegram (home chat), telegram:<chat_id> or a chat id")
    tg = sub.add_parser("telegram", help="Telegram notifications on/off (off by default; alerts always go through)")
    tg.add_argument("etat", choices=("on", "off", "etat"))
    esprit_commands.add_parser(sub, _ArgumentParser)
    todo_commands.add_parser(sub, _ArgumentParser)
    health_commands.add_parser(sub, _ArgumentParser)
    priv_commands.add_parser(sub, _ArgumentParser)
    branch_commands.add_parser(sub, _ArgumentParser)
    usage_commands.add_parser(sub, _ArgumentParser)
    backup_commands.add_parser(sub, _ArgumentParser)
    video_commands.add_parser(sub, _ArgumentParser)
    montage_commands.add_parser(sub, _ArgumentParser)
    docs_commands.add_parser(sub, _ArgumentParser)
    veille_commands.add_parser(sub, _ArgumentParser)
    depot_commands.add_parser(sub, _ArgumentParser)
    menage_commands.add_parser(sub, _ArgumentParser)
    audit_commands.add_parser(sub, _ArgumentParser)
    rapports_commands.add_parser(sub, _ArgumentParser)
    habitudes_commands.add_parser(sub, _ArgumentParser)
    routines_commands.add_parser(sub, _ArgumentParser)
    objectif_commands.add_parser(sub, _ArgumentParser)
    banc_commands.add_parser(sub, _ArgumentParser)
    groupe_commands.add_parser(sub, _ArgumentParser)
    cap_commands.add_parser(sub, _ArgumentParser)
    cap_commands.add_bilan_parser(sub, _ArgumentParser)
    cout_commands.add_parser(sub, _ArgumentParser)
    kit_commands.add_parser(sub, _ArgumentParser)
    session_commands.add_parser(sub, _ArgumentParser)
    notifs_commands.add_parser(sub, _ArgumentParser)
    recus_commands.add_parser(sub, _ArgumentParser)
    compte_commands.add_parser(sub, _ArgumentParser)
    souvenirs_commands.add_parser(sub, _ArgumentParser)
    escouade_commands.add_parser(sub, _ArgumentParser)
    prompt_commands.add_parser(sub, _ArgumentParser)
    chantier_commands.add_parser(sub, _ArgumentParser)
    bot_commands.add_parser(sub, _ArgumentParser)
    dashboard_commands.add_parser(sub, _ArgumentParser)
    return parser


BROKEN_PIPE_EXIT = 1  # the reader of stdout has gone (`ultron-hub … | head`): fixed code, no JSON re-sent


def _silence_stdout() -> None:
    """Point fd 1 at devnull so the flush at interpreter exit cannot raise « Exception ignored in <stdout> »."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    except (OSError, ValueError):
        pass


def _write_stdout(text: str, code: int) -> int:
    """Write one JSON line and return `code`. A closed reader turns into BROKEN_PIPE_EXIT, without a retry."""
    try:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
        return code
    except BrokenPipeError:
        _silence_stdout()
        return BROKEN_PIPE_EXIT


def _emit(result: JobResult) -> int:
    return _write_stdout(result.to_json(), 0 if result.ok else 1)


def _emit_payload(payload: dict) -> int:
    return _write_stdout(json.dumps(payload, ensure_ascii=False), 0 if payload["status"] == "ok" else 1)


def _main_esprit(args: argparse.Namespace) -> int:
    if args.command == "esprit" and args.esprit_command == "contexte":  # PostToolUse gauge: silence on any error
        try:
            return _emit_hook(esprit_commands.claude_hook_contexte(load_settings()))
        except BaseException:  # noqa: BLE001
            return _emit_hook({})
    hook = args.command == "esprit" and args.esprit_command == "context" and args.claude_hook
    try:
        settings = load_settings()
        setup_logging(settings.logs_dir, settings.log_level, settings.log_file,
                      settings.log_max_bytes, settings.log_backup_count)
        if hook:
            return _emit_hook(esprit_commands.claude_hook_context(settings, args.limit))
        return _emit_payload(esprit_commands.handle(args, settings))
    except BaseException as exc:  # stdout must always be valid JSON
        if hook:  # a broken journal must never disturb a Claude Code session
            return _emit_hook({})
        message = str(exc) if isinstance(exc, UltronHubError) else f"{type(exc).__name__}: {exc}"
        return _emit_payload(esprit_commands.envelope(esprit_commands.command_name(args), status="error",
                                                      summary=f"{args.command} failed", error=message))


def _emit_hook(payload: dict) -> int:
    return _write_stdout(json.dumps(payload, ensure_ascii=False), 0)


def _main_send(args: argparse.Namespace) -> int:
    from ultron_hub import envoi, notifs
    try:
        if args.genre is not None and args.genre not in notifs.GENRES:
            raise ValueError(f"unknown genre {args.genre!r} (expected: {', '.join(notifs.GENRES)})")
        text = args.text if args.text is not None else sys.stdin.read()
        settings = load_settings()
        result = envoi.envoyer(text, target=args.to, settings=settings, sujet=args.sujet,
                                 chantier=args.chantier, genre=args.genre)
    except BaseException as exc:  # noqa: BLE001 - stdout must always be valid JSON
        result = envoi.Resultat("error", f"{type(exc).__name__}: {exc}")
    return _emit_payload({"status": result.status, "summary": "message sent" if result.ok else "send failed",
                          "data": result.to_dict(), "error": None if result.ok else result.detail})


def _main_todo(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
        setup_logging(settings.logs_dir, settings.log_level, settings.log_file,
                      settings.log_max_bytes, settings.log_backup_count)
        return _emit_payload(todo_commands.handle(args, settings))
    except BaseException as exc:  # stdout must always be valid JSON
        return _emit_payload(todo_commands.envelope(args.todo_command, status="error", summary="todo failed",
                                                 error=f"{type(exc).__name__}: {exc}"))


def _main_health(args: argparse.Namespace) -> int:
    try:
        return _emit_payload(health_commands.handle(args))
    except BaseException as exc:  # stdout must always be valid JSON
        return _emit_payload(health_commands.envelope(args.health_command, status="error",
                                                   summary="health failed", error=f"{type(exc).__name__}: {exc}"))


def _main_priv(args: argparse.Namespace) -> int:
    try:
        return _emit_payload(priv_commands.handle(args))
    except BaseException as exc:  # stdout must always be valid JSON
        message = str(exc) if isinstance(exc, UltronHubError) else f"{type(exc).__name__}: {exc}"
        return _emit_payload(priv_commands.envelope(args.priv_command, status="error",
                                                    summary="priv failed", error=message))


def _main_branch(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
        setup_logging(settings.logs_dir, settings.log_level, settings.log_file,
                      settings.log_max_bytes, settings.log_backup_count)
        return _emit_payload(branch_commands.handle(args, settings))
    except BaseException as exc:  # stdout must always be valid JSON
        message = str(exc) if isinstance(exc, UltronHubError) else f"{type(exc).__name__}: {exc}"
        return _emit_payload(branch_commands.envelope(args.branch_command, status="error",
                                                      summary="branch failed", error=message))


def _main_usage(args: argparse.Namespace) -> int:
    try:
        return _emit_payload(usage_commands.handle(args, load_settings()))
    except BaseException as exc:  # stdout must always be valid JSON
        return _emit_payload(usage_commands.envelope(status="error", summary="usage failed",
                                                     error=f"{type(exc).__name__}: {exc}"))


def _main_video(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
        setup_logging(settings.logs_dir, settings.log_level, settings.log_file,
                      settings.log_max_bytes, settings.log_backup_count)
        return _emit_payload(video_commands.handle(args, settings))
    except BaseException as exc:  # stdout must always be valid JSON
        message = str(exc) if isinstance(exc, UltronHubError) else f"{type(exc).__name__}: {exc}"
        if not isinstance(exc, UltronHubError):
            get_logger("cli").warning("video command failed: %s", type(exc).__name__)
        return _emit_payload(video_commands.envelope(status="error", summary="video failed", error=message))


def _main_montage(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
        setup_logging(settings.logs_dir, settings.log_level, settings.log_file,
                      settings.log_max_bytes, settings.log_backup_count)
        return _emit_payload(montage_commands.handle(args, settings))
    except BaseException as exc:  # stdout must always be valid JSON
        message = str(exc) if isinstance(exc, UltronHubError) else f"{type(exc).__name__}: {exc}"
        if not isinstance(exc, UltronHubError):
            get_logger("cli").warning("montage command failed: %s", type(exc).__name__)
        return _emit_payload(montage_commands.envelope(status="error", summary="montage failed", error=message,
                                                       command=f"montage.{args.montage_command}"))


def _main_docs(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
        setup_logging(settings.logs_dir, settings.log_level, settings.log_file,
                      settings.log_max_bytes, settings.log_backup_count)
        return _emit_payload(docs_commands.handle(args, settings))
    except BaseException as exc:  # stdout must always be valid JSON
        message = str(exc) if isinstance(exc, UltronHubError) else f"{type(exc).__name__}: {exc}"
        return _emit_payload(docs_commands.envelope(status="error", summary="docs failed", error=message,
                                                    command=f"docs.{args.docs_command}"))


def _main_backup(args: argparse.Namespace) -> int:
    name = "heartbeat" if args.command == "heartbeat" else f"backup.{args.backup_command}"
    try:
        if args.command == "heartbeat":
            return _emit_payload(backup_commands.handle_heartbeat(args))
        settings = load_settings()
        setup_logging(settings.logs_dir, settings.log_level, settings.log_file,
                      settings.log_max_bytes, settings.log_backup_count)
        return _emit_payload(backup_commands.handle_backup(args, settings))
    except BaseException as exc:  # stdout must always be valid JSON
        return _emit_payload(backup_commands.envelope(name, status="error", summary=f"{name} failed",
                                                      error=f"{type(exc).__name__}: {exc}"))


def _main_veille(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
        setup_logging(settings.logs_dir, settings.log_level, settings.log_file,
                      settings.log_max_bytes, settings.log_backup_count)
        return _emit_payload(veille_commands.handle(args, settings))
    except BaseException as exc:  # stdout must always be valid JSON
        message = str(exc) if isinstance(exc, UltronHubError) else f"{type(exc).__name__}: {exc}"
        return _emit_payload(veille_commands.envelope(args.veille_command, status="error", summary="veille failed",
                                                      error=message))


def _main_menage(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
        setup_logging(settings.logs_dir, settings.log_level, settings.log_file,
                      settings.log_max_bytes, settings.log_backup_count)
        return _emit_payload(menage_commands.handle(args, settings))
    except BaseException as exc:  # stdout must always be valid JSON
        message = str(exc) if isinstance(exc, UltronHubError) else f"{type(exc).__name__}: {exc}"
        return _emit_payload(menage_commands.envelope(args.menage_command, status="error", summary="menage failed",
                                                      error=message))


def _main_depot(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
        setup_logging(settings.logs_dir, settings.log_level, settings.log_file,
                      settings.log_max_bytes, settings.log_backup_count)
        return _emit_payload(depot_commands.handle(args, settings))
    except BaseException as exc:  # stdout must always be valid JSON
        message = str(exc) if isinstance(exc, UltronHubError) else f"{type(exc).__name__}: {exc}"
        return _emit_payload(depot_commands.envelope(args.depot_command, status="error", summary="depot failed",
                                                     error=message))


def _main_audit(args: argparse.Namespace) -> int:
    try:
        return _emit_payload(audit_commands.handle(args, load_settings()))  # read only: no log setup
    except BaseException as exc:  # stdout must always be valid JSON
        message = str(exc) if isinstance(exc, UltronHubError) else f"{type(exc).__name__}: {exc}"
        return _emit_payload(audit_commands.envelope(args.audit_command, status="error", summary="audit failed",
                                                     error=message))


def _main_rapports(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
        setup_logging(settings.logs_dir, settings.log_level, settings.log_file,
                      settings.log_max_bytes, settings.log_backup_count)
        return _emit_payload(rapports_commands.handle(args, settings))
    except BaseException as exc:  # stdout must always be valid JSON
        message = str(exc) if isinstance(exc, UltronHubError) else f"{type(exc).__name__}: {exc}"
        return _emit_payload(rapports_commands.envelope(args.rapports_command, status="error", summary="rapports failed",
                                                        error=message))


def _main_habitudes(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
        setup_logging(settings.logs_dir, settings.log_level, settings.log_file,
                      settings.log_max_bytes, settings.log_backup_count)
        return _emit_payload(habitudes_commands.handle(args, settings))
    except BaseException as exc:  # stdout must always be valid JSON
        message = str(exc) if isinstance(exc, UltronHubError) else f"{type(exc).__name__}: {exc}"
        return _emit_payload(habitudes_commands.envelope(args.habitudes_command, status="error",
                                                         summary="habitudes failed", error=message))


def _main_objectif(args: argparse.Namespace) -> int:
    if args.objectif_command == "stop-hook":  # exception: stdout = JSON du hook Stop, ou vide (todo #498)
        try:
            sys.stdin.read()  # le JSON du hook n'est pas utilisé : l'état vit dans l'objectif
            out = objectif_commands.stop_hook(load_settings())
        except BaseException as exc:  # noqa: BLE001
            print(f"objectif stop-hook : {type(exc).__name__}: {exc}", file=sys.stderr)
            out = ""
        if out:
            sys.stdout.write(out + "\n")
            sys.stdout.flush()
        return 0
    try:
        settings = load_settings()
        return _emit_payload(objectif_commands.handle(args, settings))
    except BaseException as exc:  # stdout must always be valid JSON
        message = str(exc) if isinstance(exc, UltronHubError) else f"{type(exc).__name__}: {exc}"
        return _emit_payload(objectif_commands.envelope(args.objectif_command, status="error",
                                                        summary="objectif failed", error=message))


def _main_banc(args: argparse.Namespace) -> int:
    try:
        return _emit_payload(banc_commands.handle(args, load_settings()))
    except BaseException as exc:  # stdout must always be valid JSON
        message = str(exc) if isinstance(exc, UltronHubError) else f"{type(exc).__name__}: {exc}"
        return _emit_payload(banc_commands.envelope(args.banc_command, status="error", summary="banc failed", error=message))


def _main_routine(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
        setup_logging(settings.logs_dir, settings.log_level, settings.log_file,
                      settings.log_max_bytes, settings.log_backup_count)
        return _emit_payload(routines_commands.handle(args, settings))
    except BaseException as exc:  # stdout must always be valid JSON
        message = str(exc) if isinstance(exc, UltronHubError) else f"{type(exc).__name__}: {exc}"
        return _emit_payload(routines_commands.envelope(args.routine_command, status="error",
                                                        summary="routine failed", error=message))


def _main_groupe(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
        setup_logging(settings.logs_dir, settings.log_level, settings.log_file,
                      settings.log_max_bytes, settings.log_backup_count)
        return _emit_payload(groupe_commands.handle(args, settings))
    except BaseException as exc:  # stdout must always be valid JSON
        message = str(exc) if isinstance(exc, UltronHubError) else f"{type(exc).__name__}: {exc}"
        return _emit_payload(groupe_commands.envelope(args.groupe_command, status="error", summary="groupe failed",
                                                      error=message))


def _main_cap(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
        setup_logging(settings.logs_dir, settings.log_level, settings.log_file,
                      settings.log_max_bytes, settings.log_backup_count)
        return _emit_payload(cap_commands.handle(args, settings))
    except BaseException as exc:  # stdout must always be valid JSON
        message = str(exc) if isinstance(exc, UltronHubError) else f"{type(exc).__name__}: {exc}"
        return _emit_payload(cap_commands.envelope(args.cap_command, status="error", summary="cap failed",
                                                   error=message))


def _main_bilan(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
        return _emit_payload(cap_commands.handle_bilan(args, settings))
    except BaseException as exc:  # stdout must always be valid JSON
        message = str(exc) if isinstance(exc, UltronHubError) else f"{type(exc).__name__}: {exc}"
        return _emit_payload({"status": "error", "command": f"bilan.{args.bilan_command}", "summary": "bilan failed",
                              "data": None, "error": message})


def _main_session(args: argparse.Namespace) -> int:
    try:
        return _emit_payload(session_commands.handle(args, load_settings()))
    except BaseException as exc:  # stdout must always be valid JSON
        return _emit_payload(session_commands.envelope(args.session_command, status="error", summary="session failed",
                                                       error=f"{type(exc).__name__}: {exc}"))


def _main_notifs(args: argparse.Namespace) -> int:
    try:
        return _emit_payload(notifs_commands.handle(args, load_settings()))
    except BaseException as exc:  # stdout must always be valid JSON
        return _emit_payload(notifs_commands.envelope(args.notifs_command, status="error", summary="notifs failed",
                                                      error=f"{type(exc).__name__}: {exc}"))


def _main_joindre(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
        setup_logging(settings.logs_dir, settings.log_level, settings.log_file,
                      settings.log_max_bytes, settings.log_backup_count)
        return _emit_payload(recus_commands.handle(args, settings))
    except BaseException as exc:  # stdout must always be valid JSON
        message = str(exc) if isinstance(exc, UltronHubError) else f"{type(exc).__name__}: {exc}"
        return _emit_payload(recus_commands.envelope(f"joindre.{args.joindre_command}", status="error",
                                                     summary="joindre failed", error=message))


def _main_kit(args: argparse.Namespace) -> int:
    try:
        return _emit_payload(kit_commands.handle(args))
    except BaseException as exc:  # stdout must always be valid JSON
        return _emit_payload(kit_commands.envelope(args.kit_command, status="error", summary="kit failed",
                                                   error=f"{type(exc).__name__}: {exc}"))


def _main_cout(args: argparse.Namespace) -> int:
    try:
        return _emit_payload(cout_commands.handle(args, load_settings()))
    except BaseException as exc:  # stdout must always be valid JSON
        return _emit_payload(cout_commands.envelope(args.cout_command, status="error", summary="cout failed",
                                                    error=f"{type(exc).__name__}: {exc}"))


def _main_telegram(args: argparse.Namespace) -> int:
    from ultron_hub import envoi
    try:
        settings = load_settings()
        if args.etat != "etat":
            envoi.regler_telegram(args.etat == "on", settings)
        actif = envoi.telegram_actif(settings)
        texte = ("Telegram activé : les notifications y repartent" if actif else
                 "Telegram coupé : seules les alertes de panne y passent (le dashboard reçoit tout)")
        return _emit_payload({"status": "ok", "command": "telegram." + args.etat, "summary": texte,
                              "data": {"actif": actif, "text": texte}, "error": None})
    except BaseException as exc:  # stdout must always be valid JSON
        return _emit_payload({"status": "error", "command": "telegram." + args.etat, "summary": "telegram failed",
                              "data": None, "error": type(exc).__name__})


def _main_compte(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
        setup_logging(settings.logs_dir, settings.log_level, settings.log_file,
                      settings.log_max_bytes, settings.log_backup_count)
        return _emit_payload(compte_commands.handle(args, settings))
    except BaseException as exc:  # stdout must always be valid JSON; the message never holds a token
        return _emit_payload(compte_commands.envelope(args.compte_command, status="error", summary="compte failed",
                                                      error=type(exc).__name__))


def _main_souvenirs(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
        setup_logging(settings.logs_dir, settings.log_level, settings.log_file,
                      settings.log_max_bytes, settings.log_backup_count)
        return _emit_payload(souvenirs_commands.handle(args, settings))
    except BaseException as exc:  # stdout must always be valid JSON
        message = str(exc) if isinstance(exc, UltronHubError) else f"{type(exc).__name__}: {exc}"
        return _emit_payload(souvenirs_commands.envelope(args.souvenirs_command, status="error",
                                                         summary="souvenirs failed", error=message))


def _main_escouade(args: argparse.Namespace) -> int:
    try:
        return _emit_payload(escouade_commands.handle(args))
    except BaseException as exc:  # stdout must always be valid JSON
        message = str(exc) if isinstance(exc, UltronHubError) else f"{type(exc).__name__}: {exc}"
        return _emit_payload(escouade_commands.envelope(args.escouade_command, status="error",
                                                        summary="escouade failed", error=message))


def _main_prompt(args: argparse.Namespace) -> int:
    try:
        return _emit_payload(prompt_commands.handle(args, load_settings()))
    except BaseException as exc:  # stdout must always be valid JSON
        message = str(exc) if isinstance(exc, UltronHubError) else f"{type(exc).__name__}: {exc}"
        return _emit_payload(prompt_commands.envelope(args.prompt_command, status="error",
                                                      summary="prompt failed", error=message))


def _main_chantier(args: argparse.Namespace) -> int:
    try:
        return _emit_payload(chantier_commands.handle(args, load_settings()))
    except BaseException as exc:  # stdout must always be valid JSON
        message = str(exc) if isinstance(exc, UltronHubError) else f"{type(exc).__name__}: {exc}"
        return _emit_payload(chantier_commands.envelope(args.chantier_command, status="error",
                                                        summary="chantier failed", error=message))


def _main_bot(args: argparse.Namespace) -> int:
    try:
        payload = bot_commands.handle(args)
    except BaseException as exc:  # stdout must always be valid JSON; the token never appears in an exception text
        payload = {"status": "error", "command": "bot.run", "summary": "bot failed", "data": None,
                   "error": type(exc).__name__, "exit_code": 1}
    code = payload.pop("exit_code", 1)
    _emit_payload(payload)
    return code


def _main_dashboard(args: argparse.Namespace) -> int:
    try:
        payload = dashboard_commands.handle(args)
    except BaseException as exc:  # stdout must always be valid JSON
        payload = {"status": "error", "command": f"dashboard.{getattr(args, 'dashboard_command', 'serve')}",
                   "summary": "dashboard failed", "data": None,
                   "error": type(exc).__name__, "exit_code": 1}
    code = payload.pop("exit_code", 1)
    _emit_payload(payload)
    return code


def main(argv: list[str] | None = None) -> int:
    try:
        return _dispatch(argv)
    except BrokenPipeError:  # a write outside the _emit helpers hit a closed stdout
        _silence_stdout()
        return BROKEN_PIPE_EXIT


def _dispatch(argv: list[str] | None) -> int:
    action, job_id = "", new_job_id()
    try:
        args = build_parser().parse_args(argv)
        if args.command == "send":
            return _main_send(args)
        if args.command in esprit_commands.GROUPS:
            return _main_esprit(args)
        if args.command == "todo":
            return _main_todo(args)
        if args.command == "health":
            return _main_health(args)
        if args.command == "priv":
            return _main_priv(args)
        if args.command == "branch":
            return _main_branch(args)
        if args.command == "usage":
            return _main_usage(args)
        if args.command == "video":
            return _main_video(args)
        if args.command == "montage":
            return _main_montage(args)
        if args.command == "docs":
            return _main_docs(args)
        if args.command in ("backup", "heartbeat"):
            return _main_backup(args)
        if args.command == "veille":
            return _main_veille(args)
        if args.command == "depot":
            return _main_depot(args)
        if args.command == "menage":
            return _main_menage(args)
        if args.command == "audit":
            return _main_audit(args)
        if args.command == "rapports":
            return _main_rapports(args)
        if args.command == "habitudes":
            return _main_habitudes(args)
        if args.command == "routine":
            return _main_routine(args)
        if args.command == "objectif":
            return _main_objectif(args)
        if args.command == "banc":
            return _main_banc(args)
        if args.command == "groupe":
            return _main_groupe(args)
        if args.command == "cap":
            return _main_cap(args)
        if args.command == "bilan":
            return _main_bilan(args)
        if args.command == "cout":
            return _main_cout(args)
        if args.command == "kit":
            return _main_kit(args)
        if args.command == "session":
            return _main_session(args)
        if args.command == "notifs":
            return _main_notifs(args)
        if args.command == "joindre":
            return _main_joindre(args)
        if args.command == "telegram":
            return _main_telegram(args)
        if args.command == "compte":
            return _main_compte(args)
        if args.command == "souvenirs":
            return _main_souvenirs(args)
        if args.command == "escouade":
            return _main_escouade(args)
        if args.command == "prompt":
            return _main_prompt(args)
        if args.command == "chantier":
            return _main_chantier(args)
        if args.command == "bot":
            return _main_bot(args)
        if args.command == "dashboard":
            return _main_dashboard(args)
        action = args.action
        settings = load_settings()
        setup_logging(settings.logs_dir, settings.log_level, settings.log_file,
                      settings.log_max_bytes, settings.log_backup_count)
        try:
            params = json.loads(args.params)
        except json.JSONDecodeError as exc:
            raise InvalidRequestError(f"--params is not valid JSON: {exc}") from exc
        request = JobRequest.from_dict({
            "job_id": args.job_id, "action": args.action,
            "params": params, "requested_by": args.requested_by,
        })
        job_id = request.job_id
        return _emit(run_job(request, settings))
    except SystemExit as exc:  # --help
        return int(exc.code or 0) if isinstance(exc.code, int) else 0
    except BaseException as exc:  # stdout must always be valid JSON
        if isinstance(exc, InvalidRequestError):
            _log_refused(argv, exc)
        return _emit(JobResult(job_id=job_id, action=str(action or ""), status="error",
                               summary="request failed", error=f"{type(exc).__name__}: {exc}"))


def _log_refused(argv: list[str] | None, exc: Exception) -> None:
    """A refused command line leaves a trace (14/10: the plugin called `video collect` with an unknown option and
    nothing was logged). Only the command words are logged, never the user text that may follow."""
    words = [w for w in (sys.argv[1:] if argv is None else argv)[:3] if not w.startswith("-")][:2]
    reason = str(exc).split(":")[1].strip() if ":" in str(exc) else str(exc)  # « unrecognized arguments », no values
    try:
        settings = load_settings()
        setup_logging(settings.logs_dir, settings.log_level, settings.log_file,
                      settings.log_max_bytes, settings.log_backup_count)
        get_logger("cli").warning("refused command=%s: %s", " ".join(words), reason[:80])
    except Exception:  # logging is best effort: the JSON answer matters more
        pass


if __name__ == "__main__":
    sys.exit(main())
