"""Chantier de nuit (todo #478) : `ultron-hub chantier nuit creer | fin | attendre`. Scripts seulement, aucune IA."""

import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ultron_hub.chantier import core
from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.settings import Settings
from ultron_hub.esprit import store as journal
from ultron_hub.sessions import etat
from ultron_hub.sessions import journal as sessions_journal

NAME_RE = re.compile(r"nuit-\d\d-\d\d-\d\d")
PROJET = "{{HOME}}/workspace/projects/active/ultron-hub"
LOG_LINES = 40

REGLES = """\
Règles de la nuit (non négociables) :
- Sous-agents : jusqu'à 3 en même temps, dont UN SEUL Opus.
- Arrêt : quand la semaine atteint 85 % du quota, finir le point en cours, puis écrire le bilan.
- UN SEUL `ultron-hub send` pendant toute la nuit : le bilan final.
- Pas de pause à 90 % de la session 5 h : attendre la remise à zéro, puis reprendre.
- Passation à 60 % de contexte avec `ultron relance`.
- Avant chaque todo : `ultron-hub todo show <id>` et `git log main | grep '#<id>'` (ne rien refaire de déjà livré).
- Un sous-agent muet n'est pas éteint : attendre 45 min et vérifier son worktree (`git status`, `git log`) avant de relancer,
  sur une branche neuve.
- Ne jamais clore une todo (le ✅ est celui de l'utilisateur). Fusion automatique seulement via `ultron-hub branch auto-merge`.
- Une seule suite de tests à la fois (`pgrep -f pytest` vide avant, puis `nice -n 19`).
"""


def nom_de(day: date) -> str:
    return f"nuit-{day.day:02d}-{(day + timedelta(days=1)).day:02d}-{day.month:02d}"


def _paths(src: core.Sources, nom: str) -> dict[str, Path]:
    return {"prompt": src.prompts / f"{nom}.md", "fiche": src.prompts.parent / f"chantier-{nom}.md",
            "reprise": src.prompts / "reprises" / f"{nom}.md"}


def git_log(settings: Settings) -> str:
    out = subprocess.run(["git", "-C", str(settings.root), "log", "--oneline", f"-{LOG_LINES}", "main"],
                         capture_output=True, text=True, timeout=30, check=False)
    if out.returncode != 0:
        raise InvalidRequestError(f"git log main impossible : {out.stderr.strip()[:200]}")
    return out.stdout


def _prompt_text(day: date, nom: str) -> str:
    titre = f"Nuit {day.day:02d}-{(day + timedelta(days=1)).day:02d}/{day.month:02d}"
    return (f"# Chantier « {titre} » (temporaire)\n\nSession précédente : aucune.\n"
            f"Fiche du chantier : `~/notes/chantier-{nom}.md`\nDomaine : systeme\nProjet : {PROJET}\n\n"
            f"## 1. Mission\nTravailler cette nuit sur les todos de la fiche `~/notes/chantier-{nom}.md` (section « À faire »), "
            f"dans l'ordre, jusqu'à l'arrêt ci-dessous, puis envoyer le bilan. Ignorer la section « Déjà faites, à écarter ».\n\n"
            f"{REGLES}\n## 4. Pistes\nVoir la fiche `~/notes/chantier-{nom}.md` ; la Reprise dit où la nuit en est.\n")


def construire_fiche(settings: Settings, nom: str, log: str, now: datetime) -> str:
    rows, _has, warnings = core.open_todos(settings)
    livrees = core.delivered_ids(settings, [r["id"] for r in rows])
    vues = {r["id"] for r in rows if re.search(rf"todo #{r['id']}\b", log)}
    ecartees = [r for r in rows if r["id"] in livrees or r["id"] in vues]
    candidates = [r for r in rows if r not in ecartees]

    def groupes(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for t in items:
            out.setdefault(t["chantier"] or "Hors chantier", []).append(t)
        return out

    lines = [f"# Fiche du chantier {nom} (temporaire)", "", f"Construite le {now:%d/%m/%Y à %Hh%M}, après lecture de "
             f"`git log --oneline -{LOG_LINES} main` et des todos ouvertes.", ""]
    lines += [f"- Avertissement : {w}" for w in warnings]
    lines += ["## À faire (todos ouvertes candidates, par chantier)", ""]
    if not candidates:
        lines.append("Aucune todo candidate.")
    for chantier, items in sorted(groupes(candidates).items()):
        lines.append(f"### {chantier}")
        lines += [f"- #{t['id']} {t['title']}" for t in items]
        lines.append("")
    lines += ["## Déjà faites, à écarter", ""]
    if not ecartees:
        lines.append("Aucune.")
    for t in ecartees:
        why = "livrée" if t["id"] in livrees else "citée dans le git log de main"
        lines.append(f"- #{t['id']} {t['title']} ({why})")
    lines += ["", "## Preuve : git log brut de main", "", "```", log.rstrip(), "```", ""]
    return "\n".join(lines)


def creer(settings: Settings, src: core.Sources, day: date | None = None, log: str | None = None,
          now: datetime | None = None) -> dict[str, Any]:
    day = day or date.today()
    nom = nom_de(day)
    paths = _paths(src, nom)
    for what, p in paths.items():
        if os.path.lexists(p):
            raise InvalidRequestError(f"{nom} existe déjà ({what} : {p}) : rien n'a été écrit")
    fiche = construire_fiche(settings, nom, git_log(settings) if log is None else log, now or datetime.now())
    textes = {"prompt": _prompt_text(day, nom), "fiche": fiche, "reprise": f"## Reprise du chantier {nom}\n"}
    for key, p in paths.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textes[key], encoding="utf-8")
    journal.record(settings, "ultron-hub", "chantier", f"Chantier de nuit créé : {nom}")
    return {"nom": nom, **{k: str(v) for k, v in paths.items()}}


def _open_sessions() -> list[str]:
    return [s["nom"] for s in sessions_journal.list_sessions()]


def fin(settings: Settings, src: core.Sources, nom: str,
        open_sessions: Callable[[], list[str]] = _open_sessions) -> dict[str, Any]:
    if not NAME_RE.fullmatch(nom or ""):
        raise InvalidRequestError(f"nom refusé : {nom!r} (attendu nuit-JJ-JJ-MM)")
    ouvertes = [s for s in open_sessions() if s.startswith(f"ultron-{nom}-")]
    if ouvertes:
        raise InvalidRequestError(f"fin refusée : la session {ouvertes[0]} est encore ouverte")
    paths = _paths(src, nom)
    fait: list[str] = []
    if paths["reprise"].is_file():
        cible = src.prompts / "archives" / f"reprise-{nom}.md"
        core._move(paths["reprise"], cible)
        fait.append(f"Reprise rangée : {cible}")
    extras = [paths["prompt"], paths["fiche"], settings.root / "var" / "prompts" / f"interactif-{nom}.md",
              *sorted((src.prompts / "archives").glob(f"reprises-{nom}-*.md"))]
    for p in extras:
        if p.is_symlink() or p.is_file():
            p.unlink()
            fait.append(f"supprimé : {p}")
    journal.record(settings, "ultron-hub", "chantier", f"Chantier de nuit terminé : {nom}")
    return {"nom": nom, "fait": fait}


def attendre(own: str, max_s: float = 14400, pause_s: float = 60,
             sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
             sessions: Callable[[], list[str]] = _open_sessions,
             digest: Callable[[str], dict[str, Any]] = etat.digest) -> dict[str, Any]:
    t0 = clock()
    vues: list[str] = []
    while True:
        busy = [s for s in sessions() if s != own and digest(s).get("travaille")]
        vues += [s for s in busy if s not in vues]
        if not busy:
            return {"attendu_s": int(clock() - t0), "attendues": vues}
        if clock() - t0 >= max_s:
            return {"attendu_s": int(clock() - t0), "attendues": vues, "temps_depasse": True}
        print(f"nuit : j'attends {', '.join(busy)}", file=sys.stderr, flush=True)
        sleep(pause_s)
