"""`ultron-hub prompt interactif [<sujet>]` (todo #230): assemble the interactive orchestrator prompt. Script only, no AI.

Writes var/prompts/interactif.md = socle + interactive mode + orchestrator (+ subject sheet) + Reprise last.
With a chantier (todo #284, `ultron <chantier>`): var/prompts/interactif-<chantier>.md, and the chantier's own Reprise
(<prompts>/reprises/<chantier>.md, created with its heading when absent) instead of the orchestrator's.
"""

import argparse
import os
from pathlib import Path
from typing import Any

from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.settings import Settings
from ultron_hub.escouade import core


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None,
             error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": f"prompt.{command}", "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    prompt = sub.add_parser("prompt", help="assemble the prompts of the interactive orchestrator session")
    cmds = prompt.add_subparsers(dest="prompt_command", required=True, parser_class=parser_class)
    inter = cmds.add_parser("interactif", help="write var/prompts/interactif.md (socle + mode + orchestrator + Reprise)")
    inter.add_argument("sujet", nargs="?", default=None, help="optional subject sheet (~/notes/prompt/<sujet>.md)")
    inter.add_argument("--sortie", default=None, help="output file (default: <project>/var/prompts/interactif[-<sujet>].md)")
    inter.add_argument("--dossier", default=None, help="prompts folder (default ~/notes/prompt; tests)")

    ess = cmds.add_parser("essai", help="replay rule scenarios against the assembled prompt (costs quota)")
    ess.add_argument("sujet", help="chantier whose prompt is tested")
    ess.add_argument("--scenario", default=None, help="run only this scenario (config/prompt-essais.yaml)")
    ess.add_argument("--essai-a-blanc", action="store_true", help="call nothing: scenario count and prompt size")
    ess.add_argument("--dossier", default=None, help="prompts folder (default ~/notes/prompt; tests)")

    mes = cmds.add_parser("mesure", help="sizes in bytes of the prompt blocks and of the sheets (read only)")
    mes.add_argument("sujet", nargs="?", default=None, help="optional chantier")
    mes.add_argument("--dossier", default=None, help="prompts folder (default ~/notes/prompt; tests)")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _create_reprise(path: Path, text: str) -> bool:
    """Create the chantier's Reprise file if absent (never overwritten, links refused). True when created."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return True


def handle(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    if args.prompt_command == "essai":
        from ultron_hub.escouade import essai
        try:
            data = essai.run_essai(args.sujet, settings, scenario=args.scenario, a_blanc=args.essai_a_blanc,
                                   dossier=args.dossier)
        except InvalidRequestError as exc:
            return envelope("essai", status="error", summary="essai de prompt impossible", error=str(exc))
        return envelope("essai", status="ok" if data["ok"] else "error", summary="essai de prompt", data=data,
                        error=None if data["ok"] else "scenario failed")
    if args.prompt_command == "mesure":
        try:
            data = core.mesure(args.sujet, dossier=args.dossier)
        except InvalidRequestError as exc:  # an oversized core is refused by the assembly
            data = {"ok": False, "raison": str(exc)}
        return envelope("mesure", status="ok" if data["ok"] else "error", summary="mesure du prompt", data=data,
                        error=data["raison"])
    text = core.build_interactif(args.sujet, dossier=args.dossier)
    name = f"interactif-{args.sujet}.md" if args.sujet else "interactif.md"
    target = Path(args.sortie) if args.sortie else settings.var_dir / "prompts" / name
    _write(target, text)
    data = {"fichier": str(target), "octets": len(text.encode()), "sujet": args.sujet}
    if args.sujet:
        reprise = core.reprise_path(args.sujet, args.dossier)
        data["reprise"] = str(reprise)
        data["reprise_creee"] = _create_reprise(reprise, core.reprise_vide(args.sujet))
    return envelope(args.prompt_command, summary=f"prompt interactif écrit ({len(text.encode())} octets)", data=data)
