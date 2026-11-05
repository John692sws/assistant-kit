"""`ultron-hub audit ...` (todo #452). Read only, no AI. `inventaire [--out file.json]` prints one JSON object:
    {"status": "ok"|"error", "command": "audit.inventaire", "summary": str, "data": {elements, totaux, warnings}|null, "error": str|null}
`--out` writes the same JSON to a file (0600): the only write.
"""

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from ultron_hub.audit import inventory
from ultron_hub.common.settings import Settings
from ultron_hub.menage.rules import user_home


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None,
             error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": f"audit.{command}", "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    audit = sub.add_parser("audit", help="read only audit of the whole system (no AI)")
    cmds = audit.add_subparsers(dest="audit_command", required=True, parser_class=parser_class)
    inv = cmds.add_parser("inventaire", help="exact inventory: folders, commands, tasks, timers, skills, actions + last use")
    inv.add_argument("--out", metavar="FILE.json", help="also write the JSON here (0600)")


def write_private(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        os.fchmod(fh.fileno(), 0o600)
        fh.write(text)


def handle(args: argparse.Namespace, settings: Settings, *, home: str | None = None) -> dict[str, Any]:
    cmd = args.audit_command
    data = inventory.build(settings, home or user_home(), time.time())
    totals = data["totaux"]
    result = envelope(cmd, summary=f"{totals['elements']} élément(s), {len(data['warnings'])} avertissement(s)",
                      data=data)
    if args.out:
        try:
            write_private(args.out, json.dumps(result, ensure_ascii=False, indent=1) + "\n")
        except OSError as exc:
            data["warnings"].append(f"--out illisible : {type(exc).__name__}")
    return result
