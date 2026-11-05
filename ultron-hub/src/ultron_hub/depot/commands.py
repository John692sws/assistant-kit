"""`ultron-hub depot push` (todo #454): pushes main and the tags to the private GitHub remote.

Prints one JSON object: {"status": "ok"|"error", "command": "depot.push", "summary", "data", "error"}.
"""

import argparse
from typing import Any

from ultron_hub.common.settings import Settings
from ultron_hub.depot import push as depot_push


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None,
             error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": f"depot.{command}", "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    depot = sub.add_parser("depot", help="private GitHub remote of ultron-hub")
    cmds = depot.add_subparsers(dest="depot_command", required=True, parser_class=parser_class)
    cmds.add_parser("push", help="git push origin main --tags (never forced), records the time of the last success")


def handle(args: argparse.Namespace, settings: Settings, **kw: Any) -> dict[str, Any]:
    res = depot_push.push(settings.root, settings.var_dir, **kw)
    if res["ok"]:
        return envelope(args.depot_command, summary="main et les tags poussés", data=res)
    return envelope(args.depot_command, status="error", summary="push échoué", data=res, error=res["error"])
