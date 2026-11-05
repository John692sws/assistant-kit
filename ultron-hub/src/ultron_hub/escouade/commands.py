"""`ultron-hub escouade ...` (no Claude involved).

Every command prints one JSON object on stdout:
    {"status": "ok"|"error", "command": "escouade.<name>", "summary": str, "data": obj|null, "error": str|null}
"""

import argparse
import sys
from pathlib import Path
from typing import Any

from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.escouade import core


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None,
             error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": f"escouade.{command}", "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    escouade = sub.add_parser("escouade", help="chantier prompts: common base + subject file")
    cmds = escouade.add_subparsers(dest="escouade_command", required=True, parser_class=parser_class)
    prompt = cmds.add_parser("prompt", help="assemble the full prompt of a subject")
    prompt.add_argument("sujet")
    prompt.add_argument("--limite", required=True, help="e.g. \"3 h\"")
    prompt.add_argument("--fiche", default=None, help="default: ~/notes/chantier-<sujet>.md")
    prompt.add_argument("--drapeau", default=None, help="default: aucun (session lancée à la main)")
    prompt.add_argument("--sortie", default=None, help="also write the prompt to this file")
    prompt.add_argument("--dossier", default=None, help="prompts folder (default ~/notes/prompt; tests)")
    sujets = cmds.add_parser("sujets", help="list the subjects and their work folder")
    sujets.add_argument("--dossier", default=None)
    nouveau = cmds.add_parser("nouveau", help="create a minimal subject file; the goal is read on stdin")
    nouveau.add_argument("sujet", help="a-z, 0-9 and dashes")
    nouveau.add_argument("--titre", required=True)
    nouveau.add_argument("--dossier", default=None, help="prompts folder (default ~/notes/prompt; tests)")


def _sujet_groupe(action: str, sujet: str, dossier: Path | None = None) -> dict[str, Any]:
    """Topic of the chantier in the Telegram group (todo #203): direct mode only, never breaks the command."""
    try:
        from ultron_hub.common.settings import load_settings
        from ultron_hub.groupe import chantiers
        settings = load_settings()
        if action == "creer":
            return chantiers.creer(settings, sujet, chantiers.domaine_de(sujet, dossier))
        return chantiers.fermer(settings, sujet)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "summary": f"sujet de chantier : {type(exc).__name__}: {exc}"[:300]}


def handle(args: argparse.Namespace) -> dict[str, Any]:
    cmd = args.escouade_command
    if cmd == "sujets":
        folder = core.prompts_dir(args.dossier)
        broken = core.incomplets(folder)
        subjects = [n for n in core.list_subjects(folder) if n not in broken]
        projets = {}
        for name in subjects:
            try:
                projets[name] = str(core.projet(folder.joinpath(f"{name}.md").read_text(encoding="utf-8"))
                                    or core.DEFAULT_PROJET.expanduser())
            except (OSError, InvalidRequestError):
                projets[name] = "?"
        return envelope(cmd, summary=f"{len(subjects)} sujet(s)" + (f", {len(broken)} incomplet(s)" if broken else ""),
                        data={"sujets": subjects, "projets": projets, "incomplets": broken})
    if cmd == "nouveau":
        folder = core.prompts_dir(args.dossier)
        objectif = sys.stdin.read(core.OBJECTIF_MAX + 2)  # one char more than allowed is enough to refuse
        core.check_name(args.sujet)
        from ultron_hub.chantier.core import ARCHIVE_DIR  # chantier imports escouade: keep this one lazy
        archived = folder / ARCHIVE_DIR / f"{args.sujet}.md"
        if archived.exists():  # same name would block `chantier rouvrir` later (todo #154)
            raise InvalidRequestError(f"le sujet {args.sujet} existe déjà parmi les chantiers archivés : {archived}")
        fiche = Path(f"~/notes/chantier-{args.sujet}.md").expanduser()
        text = core.nouveau(args.sujet, args.titre, objectif, fiche_existe=fiche.is_file())
        target = folder / f"{args.sujet}.md"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("x", encoding="utf-8") as fh:  # never overwrites an existing subject
                fh.write(text)
        except FileExistsError:
            raise InvalidRequestError(f"le sujet {args.sujet} existe déjà : {target}") from None
        try:
            core.build(args.sujet, "1 h", dossier=args.dossier)  # must assemble with the common base
        except Exception:
            target.unlink(missing_ok=True)
            raise
        topic = _sujet_groupe("creer", args.sujet, folder)
        return envelope(cmd, summary=f"sujet {args.sujet} créé", data={
            "sujet": args.sujet, "fichier": str(target), "projet": str(core.projet(text)), "groupe": topic})
    if cmd == "prompt":
        text = core.build(args.sujet, args.limite, fiche=args.fiche, drapeau=args.drapeau, dossier=args.dossier)
        data: dict[str, Any] = {"sujet": args.sujet, "text": text}
        if args.sortie:
            out = Path(args.sortie).expanduser()
            out.write_text(text, encoding="utf-8")
            data["sortie"] = str(out)
        return envelope(cmd, summary=f"prompt {args.sujet} assemblé ({len(text)} caractères)", data=data)
    return envelope(cmd, status="error", error=f"unknown command: {cmd}")
