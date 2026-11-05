"""`ultron-hub cout session <transcript.jsonl>` and `cout compare`: read only, no AI, no network.

Prints one JSON object on stdout:
    {"status": "ok"|"error", "command": "cout.<name>", "summary": str, "data": obj|null, "error": str|null}
"""

import argparse
from pathlib import Path
from typing import Any

from ultron_hub.common.settings import Settings
from ultron_hub.cout import compare, session, usage_jsonl


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None,
             error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": f"cout.{command}", "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    cout = sub.add_parser("cout", help="cost of Claude sessions (read only, no AI)")
    cmds = cout.add_subparsers(dest="cout_command", required=True, parser_class=parser_class)
    one = cmds.add_parser("session", help="tokens, estimated cost, duration, delivered todos of an interactive transcript")
    one.add_argument("transcript", help="~/.claude/projects/<project>/<id>.jsonl (its subagents/ folder is read too)")
    cmp = cmds.add_parser("compare", help="last N sessions of each mode (squad, interactive): cost per delivered todo")
    cmp.add_argument("--n", type=int, default=5, help="sessions per mode (default 5)")
    cmp.add_argument("--etat-dir", default=None, help="squads folder (default ~/.local/state/ultron-hub/escouades)")
    cmp.add_argument("--projets-dir", default=None, help="Claude projects folder (default ~/.claude/projects)")


def handle(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    cmd = args.cout_command
    if cmd == "session":
        path = Path(args.transcript).expanduser()
        if not path.is_file():
            return envelope(cmd, status="error", summary="transcript introuvable", error=f"not a file: {path}")
        data = session.measure(path, settings.root)
        return envelope(cmd, summary=f"{data['cout_usd']} USD, {data['duree_s']} s, {len(data['todos_livrees'])} todo(s) livrée(s)",
                        data=data)
    if cmd == "compare":
        if args.n < 1:
            return envelope(cmd, status="error", summary="--n doit être au moins 1", error="n < 1")
        projects = Path(args.projets_dir).expanduser() if args.projets_dir else compare.DEFAULT_PROJECTS.expanduser()
        data = compare.compare(args.n, usage_jsonl.state_root(args.etat_dir), projects, settings.root)
        return envelope(cmd, summary=data["text"], data=data)
    return envelope(cmd, status="error", error=f"unknown command: {cmd}")
