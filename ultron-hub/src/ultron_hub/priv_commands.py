"""`ultron-hub priv ...`: state of the root helper (ultron-priv) and command previews (also of ultron-user, the
server actions without sudo). Never runs any action."""

import argparse
from typing import Any

from ultron_hub import privileged, user_actions


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None,
             error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": f"priv.{command}", "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    priv = sub.add_parser("priv", help="root helper (ultron-priv) state and previews")
    cmds = priv.add_subparsers(dest="priv_command", required=True, parser_class=parser_class)
    cmds.add_parser("status", help="installed, up to date, root-owned, sudo rule active")
    preview = cmds.add_parser("preview", help="exact command root would run for an action")
    preview.add_argument("action")
    preview.add_argument("args", nargs="*")
    upreview = cmds.add_parser("user-preview", help="exact commands ultron-user (no sudo) would run for an action")
    upreview.add_argument("action")
    upreview.add_argument("args", nargs="*")


def handle(args: argparse.Namespace) -> dict[str, Any]:
    cmd = args.priv_command
    if cmd == "status":
        state = privileged.status()
        ready = state["installed"] and state["up_to_date"] and state["root_owned"] and state["sudo_ok"]
        summary = "ultron-priv prêt" if ready else "ultron-priv à (ré)installer : sudo sh deploy/priv/install.sh"
        return envelope(cmd, status="ok" if ready else "error", summary=summary, data=state,
                        error=None if ready else summary)
    if cmd == "preview":
        lines = privileged.preview(args.action, list(args.args))
        return envelope(cmd, summary=" / ".join(lines), data={"command": lines})
    if cmd == "user-preview":
        lines = user_actions.preview(args.action, list(args.args))
        return envelope(cmd, summary=" / ".join(lines), data={"command": lines})
    return envelope(cmd, status="error", error=f"unknown command: {cmd}")
