"""`ultron-hub cap ...` subcommands. `collect` is read only and never calls an AI.

Every command prints one JSON object on stdout:
    {"status": "ok"|"error", "command": "cap.<name>", "summary": str, "data": obj|null, "error": str|null}
"""

import argparse
from typing import Any

from ultron_hub.cap import bilan, bilan_soir, collect, report
from ultron_hub.common.settings import Settings


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None,
             error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": f"cap.{command}", "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    cap = sub.add_parser("cap", help="weekly cap review (collect = the facts of the week, no AI, read only)")
    cmds = cap.add_subparsers(dest="cap_command", required=True, parser_class=parser_class)
    cmds.add_parser("collect", help="gather the facts of the last 7 days (JSON, 40 KB at most)")
    rep = cmds.add_parser("report", help="opus judges the facts of the week: verdict per chantier, 3 corrections at most "
                                   "(applies nothing; never skipped on quota)")
    cmds.add_parser("last", help="last successful review (var/cap/last.json): Telegram text and corrections, read only")
    rep.add_argument("--send", action="store_true", help="send the short report on Telegram (hermes send)")


def add_bilan_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    top = sub.add_parser("bilan", help="what the last 7 days really were, counted without AI")
    cmds = top.add_subparsers(dest="bilan_command", required=True, parser_class=parser_class)
    sem = cmds.add_parser("semaine", help="todos by nature, buttons used, timers versus AI jobs, week quota")
    sem.add_argument("--send", action="store_true", help="send the short text on Telegram")
    soir = cmds.add_parser("soir", help="merges and closures done alone (by auto:...) in a day, read only")
    soir.add_argument("--date", default=None, help="AAAA-MM-JJ (default: today)")


def handle_bilan(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    if args.bilan_command == "soir":
        data = bilan_soir.run(settings, args.date)
        out = envelope("bilan", summary=f"{data['date']} : {data['fusions']} fusion(s), {data['clotures']} clôture(s) seules ({data['gestes_evites']} gestes évités, {data['faits']} faits)", data=data)
        out["command"] = "bilan.soir"
        return out
    data = bilan.run(settings)
    if getattr(args, "send", False):
        data.update(bilan.send(settings, data))
    out = envelope("bilan", summary=data["text"].split("\n")[1], data=data)
    out["command"] = f"bilan.{args.bilan_command}"
    return out


def handle(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    cmd = args.cap_command
    if cmd == "collect":
        data = collect.collect(settings)
        return envelope(cmd, summary=f"{len(data['chantiers'])} chantier(s), {len(data['commits'])} commit(s), "
                                     f"{len(data['non_revus'])} non revu(s), {len(data['warnings'])} avertissement(s)",
                        data=data)
    if cmd == "last":
        data = report.last(settings)
        return envelope(cmd, summary=f"revue du {data['date']}" if data["exists"] else "pas encore de revue", data=data)
    if cmd == "report":
        data = report.run(settings)
        if getattr(args, "send", False):
            data.update(report.send(settings, data))
        return envelope(cmd, summary=data["summary"], data=data)
    return envelope(cmd, status="error", error=f"unknown command: {cmd}")
