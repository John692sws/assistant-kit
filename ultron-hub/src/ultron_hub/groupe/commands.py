"""`ultron-hub groupe tableau`: refresh the pinned boards of the Telegram group topics (todo #202)."""

import argparse
from typing import Any

from ultron_hub.common.settings import Settings
from ultron_hub.groupe import sujets, tableau


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None, error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": f"groupe.{command}", "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    groupe = sub.add_parser("groupe", help="Telegram group with topics")
    cmds = groupe.add_subparsers(dest="groupe_command", required=True, parser_class=parser_class)
    cmds.add_parser("tableau", help="send/pin/edit the board of each domain topic (direct mode only)")
    cmds.add_parser("creer", help="create the missing topics of the group (domains, then chantiers); idempotent (todo #246)")


def handle(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    if args.groupe_command == "creer":
        res = sujets.creer(settings)
        return envelope("creer", status="ok" if res["status"] == "ok" else "error", summary=res["summary"], data=res,
                        error=None if res["status"] == "ok" else res["summary"])
    res = tableau.rafraichir(settings)
    return envelope(args.groupe_command, status="error" if res["status"] == "error" else "ok",
                    summary=res["summary"], data=res, error=res["summary"] if res["status"] == "error" else None)
