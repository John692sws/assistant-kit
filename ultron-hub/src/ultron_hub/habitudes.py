"""💡 Nouvelle habitude proposée (todo #429): the skill drafts of `fin-de-chantier` installed or thrown away from Moi.

The skill `~/.claude/skills/fin-de-chantier/` (proposer.py) writes drafts in `~/ultron/propositions-skills/<date>-<nom>/`:
BROUILLON.md (« Statut : EN ATTENTE »), RAISON.md, then SKILL.md (mode `nouvelle`), PATCH.md (mode `patch`, text added
to a home-made skill) or REMPLACE.md (mode `remplace`, the WHOLE new SKILL.md of an existing skill; the old one is kept
as SKILL.md.bak-<folder of the draft>). Decision of the user (27/10): the card shows the FULL text that will be installed, and the ✅ is
signed on the fingerprint of that exact text (text changed = refused, nothing installed).

- `habitudes liste` (read only): the pending drafts, each {dossier, nom, mode, resume, texte, lignes, empreinte, complet,
  trop_long}; `empreinte` = 16 hex of the SHA-256 of JSON [mode, nom, texte]; `complet` false while « A COMPLÉTER » remains.
- `habitudes voir --dossier D`: one draft (the sheet of the dashboard reads it again, `detail` of the catalogue).
- `habitudes installer --dossier D --empreinte E`: signed `habitude` on "<dossier>:<empreinte>" (or `--i-am-human`),
  recomputed under a lock; refused if the fingerprint differs, the draft is incomplete or too long, the name is not
  `[a-z0-9-]+`, the folder is not a plain folder of the drafts, a file is a symbolic link, the target is `synced` or
  fin-de-chantier, or (nouvelle) the skill exists, or (patch, remplace) it does not. Then the draft is marked APPLIQUÉ and moved to `appliquees/`.
- `habitudes jeter --dossier D` (simple, not signed): marked JETÉ and moved to `jetees/`, nothing deleted.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.settings import Settings
from ultron_hub.esprit import store as journal
from ultron_hub.todo import closing

ACTION = "habitude"
PROPOSITIONS_ENV = "ULTRON_PROPOSITIONS_SKILLS_DIR"
SKILLS_ENV = "ULTRON_SKILLS_DIR"
DOSSIER_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9-]{1,60}")  # proposer.py: <date>-<nom>, signed target <= 96
NOM_RE = re.compile(r"[a-z0-9-]+")
EMPREINTE_RE = re.compile(r"[0-9a-f]{16}")
INTERDITES = ("synced", "fin-de-chantier")  # skills from Anthropic, and the proposer itself
A_COMPLETER = re.compile(r"[AÀ] COMPL[ÉE]TER", re.I)
LIGNES_MAX = 300  # what the sheet can list (dashboard/actions.py reads these): above = refused, never cut
LIGNE_MAX = 500
APPLIQUEES, JETEES = "appliquees", "jetees"
_ENTETE = re.compile(r"^# Brouillon (nouvelle|patch|remplace) : (\S+)", re.M)
_CIBLE_PATCH = re.compile(r"~/\.claude/skills/([^/\s]+)/SKILL\.md")
_NAME_FM = re.compile(r"\A---\n(?:.*\n)*?name:\s*([^\n]+)\n", re.M)
_STATUT = re.compile(r"Statut : EN ATTENTE[^\n]*")


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None,
             error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": f"habitudes.{command}", "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    top = sub.add_parser("habitudes", help="skill drafts of fin-de-chantier: list, install (signed ✅), throw away")
    cmds = top.add_subparsers(dest="habitudes_command", required=True, parser_class=parser_class)
    cmds.add_parser("liste", help="pending drafts with their full text and fingerprint (read only)")
    voir = cmds.add_parser("voir", help="one pending draft (read only)")
    voir.add_argument("--dossier", required=True)
    ins = cmds.add_parser("installer", help="install the draft's exact text (signed habitude on <dossier>:<empreinte>)")
    ins.add_argument("--dossier", required=True)
    ins.add_argument("--empreinte", required=True, help="fingerprint shown by `habitudes liste`")
    ins.add_argument("--i-am-human", dest="human", action="store_true",
                     help="confirm at a real terminal (typed code) instead of a signed button")
    jet = cmds.add_parser("jeter", help="move the draft to jetees/ (nothing deleted)")
    jet.add_argument("--dossier", required=True)


def propositions_dir() -> Path:
    override = os.environ.get(PROPOSITIONS_ENV)
    return Path(override) if override else Path.home() / "ultron" / "propositions-skills"


def skills_dir() -> Path:
    override = os.environ.get(SKILLS_ENV)
    return Path(override) if override else Path.home() / ".claude" / "skills"


def var(settings: Settings) -> Path:
    return settings.var_dir / "habitudes"


def _refuse(reason: str) -> None:
    raise InvalidRequestError(f"❌ Habitude refusée : {reason}\n\nRien n'a été installé. "
                              "À faire : rouvre la carte « Nouvelle habitude proposée » (onglet Moi) pour voir l'état à jour.")


def _lire(path: Path) -> str | None:
    if path.is_symlink():
        _refuse(f"{path.name} est un lien symbolique")
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _dossier(name: str) -> Path:
    """The draft folder named `name`: a plain folder right under the drafts folder, or refused."""
    if DOSSIER_RE.fullmatch(name or "") is None:
        _refuse("nom de dossier invalide (<date>-<nom>)")
    base = propositions_dir()
    path = base / name
    if path.is_symlink():
        _refuse("le dossier est un lien symbolique")
    if not path.is_dir():
        _refuse("brouillon introuvable (déjà installé ou jeté ?)")
    if path.resolve().parent != base.resolve():
        _refuse("dossier hors de ~/ultron/propositions-skills")
    return path


def empreinte(mode: str, nom: str, texte: str) -> str:
    return hashlib.sha256(json.dumps([mode, nom, texte], ensure_ascii=False).encode()).hexdigest()[:16]


def _resume(mode: str, nom: str, jour: str, raison: str) -> str:
    quoi = {"nouvelle": "Nouvelle skill", "patch": "Ajout à la skill", "remplace": "Remplacement de la skill"}[mode]
    first = f"{quoi} « {nom} », proposée le {jour}."
    sections: dict[str, list[str]] = {}
    current = ""
    for line in raison.splitlines():
        if line.startswith("## "):
            current = line[3:].strip().lower()
            sections[current] = []
        elif current:
            sections[current].append(line.strip())
    count = lambda key: sum(1 for s, lines in sections.items() if key in s  # noqa: E731
                            for x in lines if x.startswith("- ") and x != "- (aucun)")
    gestes = next((x for s, lines in sections.items() if "geste" in s for x in lines if x and x != "(aucun)"), "")
    gestes = ", ".join(g.strip() for g in gestes.split(",")[:3])
    second = f"Tirée de {count('correction')} correction(s) et {count('piège')} piège(s)" + \
        (f" ; gestes répétés : {gestes}." if gestes else ".")
    return f"{first} {second}"


def lire_brouillon(path: Path) -> dict[str, Any] | None:
    """The draft of `path`, or None when it is not pending (no BROUILLON.md « EN ATTENTE »)."""
    brouillon = _lire(path / "BROUILLON.md")
    if brouillon is None or "Statut : EN ATTENTE" not in brouillon:
        return None
    skill, patch, remplace = _lire(path / "SKILL.md"), _lire(path / "PATCH.md"), _lire(path / "REMPLACE.md")
    head = _ENTETE.search(brouillon)
    mode = head.group(1) if head else ("nouvelle" if skill is not None else "remplace" if remplace is not None else "patch")
    if mode == "nouvelle":
        texte = skill
        fm = _NAME_FM.search(skill or "")
        nom = head.group(2) if head else (fm.group(1).strip().strip("\"'") if fm else "")
    elif mode == "remplace":
        texte = remplace
        nom = head.group(2) if head else ""
    else:
        texte = patch
        cible = _CIBLE_PATCH.search(patch or "")
        nom = cible.group(1) if cible else (head.group(2) if head else "")
        if patch is not None:
            parts = re.split(r"^À ajouter[^\n]*\n", patch, maxsplit=1, flags=re.M)
            texte = parts[1].lstrip("\n") if len(parts) == 2 else patch
    if texte is None:
        texte = ""
    lignes = texte.splitlines()
    jour = path.name[:10]
    return {"dossier": path.name, "nom": nom, "mode": mode,
            "resume": _resume(mode, nom, jour, _lire(path / "RAISON.md") or ""),
            "texte": texte, "lignes": lignes, "empreinte": empreinte(mode, nom, texte),
            "complet": bool(texte.strip()) and not A_COMPLETER.search(texte),
            "trop_long": len(lignes) > LIGNES_MAX or any(len(x) > LIGNE_MAX for x in lignes)}


def liste() -> list[dict[str, Any]]:
    base = propositions_dir()
    if not base.is_dir():
        return []
    out = []
    for path in sorted(base.iterdir()):
        if path.is_symlink() or not path.is_dir() or DOSSIER_RE.fullmatch(path.name) is None:
            continue
        try:
            b = lire_brouillon(path)
        except InvalidRequestError:  # a symbolic link inside: never shown, never installed
            continue
        if b is not None:
            out.append(b)
    return out


def voir(name: str) -> dict[str, Any]:
    b = lire_brouillon(_dossier(name))
    if b is None:
        _refuse("ce brouillon n'est plus en attente")
    return b  # type: ignore[return-value]


def _cible(nom: str) -> Path:
    if NOM_RE.fullmatch(nom or "") is None:
        _refuse(f"nom de skill invalide ({nom!r} : lettres minuscules, chiffres et tirets seulement)")
    if nom in INTERDITES:
        _refuse(f"la skill « {nom} » ne se modifie jamais d'ici")
    base = skills_dir()
    target = base / nom
    if target.is_symlink() or (target / "SKILL.md").is_symlink():
        _refuse("la skill visée est un lien symbolique")
    if target.exists() and target.resolve().parent != base.resolve():
        _refuse("skill hors de ~/.claude/skills")
    return target


def proof_used(settings: Settings, proof: str) -> bool:
    try:
        lines = (var(settings) / "installees.jsonl").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return False
    for line in lines:
        try:
            if json.loads(line).get("proof") == proof:
                return True
        except ValueError:
            continue
    return False


def _ranger(path: Path, sous: str, statut: str) -> Path:
    """Mark BROUILLON.md then move the folder to `sous/` (a suffix if the name is taken): nothing is ever deleted."""
    fichier = path / "BROUILLON.md"
    texte = fichier.read_text(encoding="utf-8")
    fichier.write_text(_STATUT.sub(f"Statut : {statut}", texte, count=1), encoding="utf-8")
    dest_dir = propositions_dir() / sous
    dest_dir.mkdir(exist_ok=True)
    dest, n = dest_dir / path.name, 2
    while dest.exists():
        dest, n = dest_dir / f"{path.name}-{n}", n + 1
    path.rename(dest)
    return dest


def installer(settings: Settings, name: str, expected: str, authorize: Callable[[dict[str, Any]], dict[str, Any]]
              ) -> dict[str, Any]:
    if EMPREINTE_RE.fullmatch(expected or "") is None:
        _refuse("empreinte invalide (16 caractères hexadécimaux)")
    var(settings).mkdir(parents=True, exist_ok=True)
    with open(var(settings) / "lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = _dossier(name)
        b = lire_brouillon(path)
        if b is None:
            _refuse("ce brouillon n'est plus en attente")
        assert b is not None
        if b["empreinte"] != expected:
            _refuse("le texte du brouillon a changé depuis son affichage")
        if not b["complet"]:
            _refuse("brouillon à compléter (« A COMPLÉTER » ou texte vide)")
        if b["trop_long"]:
            _refuse(f"texte trop long pour être relu sur la feuille ({LIGNES_MAX} lignes de {LIGNE_MAX} caractères au plus)")
        target = _cible(b["nom"])
        if b["mode"] == "nouvelle" and (target.exists() or target.is_symlink()):
            _refuse(f"la skill « {b['nom']} » existe déjà (propose un patch)")
        if b["mode"] in ("patch", "remplace") and not (target / "SKILL.md").is_file():
            _refuse(f"la skill « {b['nom']} » visée n'existe pas")
        by = authorize(b)
        if by.get("proof") and proof_used(settings, by["proof"]):
            raise closing.CloseRefused("jeton de l'habitude déjà utilisé")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with (var(settings) / "installees.jsonl").open("a", encoding="utf-8") as fh:  # spent before anything is written
            fh.write(json.dumps({"at": now, "by": by.get("by"), "proof": by.get("proof"), "dossier": name,
                                 "nom": b["nom"], "mode": b["mode"], "empreinte": expected}, ensure_ascii=False) + "\n")
        texte = b["texte"] if b["texte"].endswith("\n") else b["texte"] + "\n"
        if b["mode"] == "nouvelle":
            target.mkdir(mode=0o755)
            os.chmod(target, 0o755)
            fd = os.open(target / "SKILL.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(texte)
            os.chmod(target / "SKILL.md", 0o644)
        elif b["mode"] == "remplace":
            old, bak = target / "SKILL.md", target / f"SKILL.md.bak-{name}"
            if bak.exists() or bak.is_symlink():
                _refuse("la sauvegarde de l'ancienne version existe déjà")
            shutil.copy2(old, bak)  # the old version is kept before anything is replaced
            tmp = target / "SKILL.md.new"
            tmp.write_text(texte, encoding="utf-8")
            os.chmod(tmp, 0o644)
            os.replace(tmp, old)
        else:
            with (target / "SKILL.md").open("a", encoding="utf-8") as fh:
                fh.write(f"\n## Ajout du {now[:10]} (brouillon {name})\n\n{texte}")
        dest = _ranger(path, APPLIQUEES, f"APPLIQUÉ le {now} (empreinte {expected}, par {by.get('by')}).")
    quoi = {"nouvelle": "installée", "remplace": "remplacée"}.get(b["mode"], "complétée")
    text = f"💡 Skill « {b['nom']} » {quoi} : ~/.claude/skills/{b['nom']}/SKILL.md"
    journal.record(settings, "ultron-hub", "habitude", f"{text} (brouillon {name}, {by.get('by')})")
    return {"dossier": name, "nom": b["nom"], "mode": b["mode"], "empreinte": expected, "range": str(dest), "text": text}


def jeter(settings: Settings, name: str) -> dict[str, Any]:
    var(settings).mkdir(parents=True, exist_ok=True)
    with open(var(settings) / "lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = _dossier(name)
        b = lire_brouillon(path)
        if b is None:
            _refuse("ce brouillon n'est plus en attente")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        dest = _ranger(path, JETEES, f"JETÉ le {now} depuis le dashboard.")
    text = f"🗑 Brouillon d'habitude « {b['nom']} » jeté (gardé dans jetees/)"  # type: ignore[index]
    journal.record(settings, "ultron-hub", "habitude", f"{text} : {name}")
    return {"dossier": name, "range": str(dest), "text": text}


def handle(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    cmd = args.habitudes_command
    if cmd == "liste":
        items = liste()
        return envelope(cmd, summary=f"{len(items)} habitude(s) proposée(s)" if items else "aucune habitude proposée",
                        data={"habitudes": items})
    if cmd == "voir":
        b = voir(args.dossier)
        return envelope(cmd, summary=f"{b['mode']} : {b['nom']}", data=b)
    if cmd == "installer":
        def authorize(b: dict[str, Any]) -> dict[str, Any]:
            what = (f"Installer la skill « {b['nom']} » ({b['mode']}) avec ce texte :\n\n{b['texte']}\n?")
            return closing.authorize(ACTION, f"{args.dossier}:{args.empreinte}", human=args.human, what=what,
                                     used=lambda proof: proof_used(settings, proof))
        data = installer(settings, args.dossier, args.empreinte, authorize)
        return envelope(cmd, summary=data["text"], data=data)
    if cmd == "jeter":
        data = jeter(settings, args.dossier)
        return envelope(cmd, summary=data["text"], data=data)
    return envelope(cmd, status="error", error=f"unknown command: {cmd}")
