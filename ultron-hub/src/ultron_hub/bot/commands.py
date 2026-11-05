"""`ultron-hub bot run` (todo #193) : le bot Telegram à nous, au premier plan (lancé par ultron-bot.service)."""

import argparse
from typing import Any


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    bot = sub.add_parser("bot", help="bot Telegram direct, sans Hermes (jeton : TELEGRAM_BOT_TOKEN de l'environnement)")
    cmds = bot.add_subparsers(dest="bot_command", required=True, parser_class=parser_class)
    cmds.add_parser("run", help="long polling au premier plan, jusqu'à SIGTERM (ultron-bot.service)")


def handle(args: argparse.Namespace) -> dict[str, Any]:
    from ultron_hub.bot import run

    return run.run()
