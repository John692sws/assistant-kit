"""`ultron-hub kit scan|export`: personal data detector and allowlist export, no AI, no network.

Prints one JSON object on stdout: {"status", "command": "kit.scan", "summary", "data": {"findings", "count"}, "error"}.
Exit code 1 on any finding or refusal. Findings never carry the matched value.
"""

import argparse
from pathlib import Path
from typing import Any

from ultron_hub.kit import detect, export


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None,
             error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": f"kit.{command}", "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    kit = sub.add_parser("kit", help="public kit export tools (read only, no AI)")
    cmds = kit.add_subparsers(dest="kit_command", required=True, parser_class=parser_class)
    scan = cmds.add_parser("scan", help="find personal data in a folder (exit 1 if any; values are never printed)")
    scan.add_argument("dossier", help="folder to scan")
    scan.add_argument("--interdits", default=None, help=f"forbidden list (default {detect.INTERDITS_PATH})")
    exp = cmds.add_parser("export", help="copy the allowlisted files of main into <out>/ultron-hub, then scan them")
    exp.add_argument("sortie", help="output folder (must be absent or empty)")
    exp.add_argument("--interdits", default=None, help=f"forbidden list (default {detect.INTERDITS_PATH})")
    exp.add_argument("--essai", action="store_true", help="dry run: list kept/excluded/unclassified, write nothing")
    exp.add_argument("--depot", default=str(export.PROJECT_ROOT), help=argparse.SUPPRESS)
    exp.add_argument("--ref", default=export.DEFAULT_REF, help=argparse.SUPPRESS)
    exp.add_argument("--config", default=str(export.DEFAULT_CONFIG), help=argparse.SUPPRESS)


def _export(args: argparse.Namespace) -> dict[str, Any]:
    cmd = args.kit_command
    try:
        plan = export.plan_export(Path(args.depot), args.ref, Path(args.config))
        counts = {"retenus": len(plan.kept), "exclus": len(plan.excluded), "non_classes": len(plan.unclassified)}
        if args.essai:
            data = {"essai": True, **counts, "fichiers_retenus": plan.kept, "fichiers_exclus": plan.excluded,
                    "non_classes": plan.unclassified}
            return envelope(cmd, summary=f"essai : {counts['retenus']} retenus, {counts['non_classes']} non classés",
                            data=data)
        out = Path(args.sortie).expanduser()
        target = export.check_target(out)
        patterns = detect.load_interdits(Path(args.interdits).expanduser() if args.interdits else None)
        table = export.load_replacements(Path(args.config))
        export.write_tree(Path(args.depot), args.ref, plan, target)
        export.apply_replacements(target, table)
    except (export.ExportError, detect.RefusedError) as exc:
        return envelope(cmd, status="error", summary="export refusé", error=str(exc))
    findings = [{"path": f.path, "line": f.line, "rule": f.rule} for f in detect.scan(target.parent, patterns)]
    data = {"essai": False, "dossier": str(target), **counts, "non_classes": plan.unclassified,
            "findings": findings, "count": len(findings), "publiable": not findings}
    if findings:
        return envelope(cmd, status="found", summary=f"{len(findings)} trouvaille(s), non publiable", data=data)
    return envelope(cmd, summary=f"{counts['retenus']} fichiers exportés, aucune trouvaille", data=data)


def handle(args: argparse.Namespace) -> dict[str, Any]:
    cmd = args.kit_command
    if cmd == "export":
        return _export(args)
    root = Path(args.dossier).expanduser()
    if not root.is_dir():
        return envelope(cmd, status="error", summary="dossier introuvable", error=f"not a directory: {root}")
    try:
        patterns = detect.load_interdits(Path(args.interdits).expanduser() if args.interdits else None)
    except detect.RefusedError as exc:
        return envelope(cmd, status="error", summary="scan refusé : liste d'interdits absente ou vide", error=str(exc))
    findings = [{"path": f.path, "line": f.line, "rule": f.rule} for f in detect.scan(root, patterns)]
    data = {"findings": findings, "count": len(findings)}
    if findings:
        return envelope(cmd, status="found", summary=f"{len(findings)} trouvaille(s)", data=data)
    return envelope(cmd, summary="aucune donnée personnelle trouvée", data=data)
