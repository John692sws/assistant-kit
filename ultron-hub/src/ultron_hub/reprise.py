"""Todo #501: reprise d'un objectif ouvert dont la session tmux a disparu ou est bloquée par le quota.

Appelée à la main (`ultron-hub objectif reprendre [--essai]`) et par le heartbeat (une fois par passage).
Aucun shell, binaires en chemins absolus ; tmux, ultron et usage sont injectables (tests sans vrai tmux ni claude).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ultron_hub import objectif
from ultron_hub.common.settings import Settings

TMUX = "/usr/bin/tmux"
ULTRON = str(Path.home() / ".local" / "bin" / "ultron")
REPRISES_MAX = 3
FENETRE = timedelta(hours=24)
DELAI_MIN = timedelta(minutes=30)
QUOTA_MAX_PCT = 95.0
MOTIFS_QUOTA = re.compile(r"usage limit|limit reached|resets", re.IGNORECASE)
CHANTIER_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
NOM_RE = re.compile(r"Session tmux : (\S+)")
PREFIXE = "reprise : "


def tmux_reel(argv: list[str]) -> tuple[int, str]:
    """Lance tmux (argv sans le binaire). rc 127 si tmux est introuvable."""
    try:
        p = subprocess.run([TMUX, *argv], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return p.returncode, p.stdout


def ultron_reel(argv: list[str]) -> tuple[int, str]:
    """Lance `ultron` sans terminal ni TMUX : la session est créée détachée, l'attache finale échoue sans gravité."""
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    try:
        p = subprocess.run([ULTRON, *argv], capture_output=True, text=True, timeout=90, check=False,
                           stdin=subprocess.DEVNULL, env=env, start_new_session=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return p.returncode, p.stdout


def usage_reel(settings: Settings) -> dict[str, Any]:
    from ultron_hub.usage.collect import usage
    return usage(settings.var_dir / "cache" / "usage.json")


def _quota_revenu(usage: dict[str, Any]) -> tuple[bool, str]:
    if usage.get("stale"):
        return False, "usage périmé"
    pct = (usage.get("session") or {}).get("percent")
    if not isinstance(pct, (int, float)):
        return False, "usage sans pourcentage"
    if pct >= QUOTA_MAX_PCT:
        return False, f"quota pas revenu ({pct:.0f} %)"
    return True, ""


def _date(texte: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(texte)
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _garde_fous(obj: dict[str, Any], now: datetime) -> str:
    """Raison du refus ('' = autorisé), d'après les preuves « reprise : … » (échecs compris)."""
    dates = [d for p in obj.get("preuves", []) if p.get("texte", "").startswith(PREFIXE)
             if (d := _date(p.get("le", ""))) is not None]
    if len([d for d in dates if now - d < FENETRE]) >= REPRISES_MAX:
        return f"plafond atteint ({REPRISES_MAX} reprises en 24 h)"
    if dates and now - max(dates) < DELAI_MIN:
        return "dernière reprise il y a moins de 30 min"
    return ""


def _chantier(obj: dict[str, Any]) -> str | None:
    c = obj.get("chantier") or ""
    if not CHANTIER_RE.fullmatch(c) or c in ("main", "nuit", "liste", "list", "relance"):
        return None
    dossier = Path(os.environ.get("ULTRON_PROMPTS_DIR") or Path.home() / "notes" / "prompt")
    return c if (dossier / f"{c}.md").is_file() else None


def commande(obj: dict[str, Any]) -> list[str]:
    msg = (f"Reprends l'objectif {obj['id']} (ultron-hub objectif show {obj['id']}) "
           f"à l'étape « {obj['etape']} ».")
    c = _chantier(obj)
    return [ULTRON, *(["--chantier", c] if c else []), msg]


def reprendre(settings: Settings, essai: bool = False, *, tmux: Callable[[list[str]], tuple[int, str]] = tmux_reel,
              lanceur: Callable[[list[str]], tuple[int, str]] = ultron_reel,
              usage: Callable[[], dict[str, Any]] | None = None,
              now: datetime | None = None) -> dict[str, Any]:
    """Plan (essai) ou exécution. `plan` : une entrée par objectif coupé ou bloqué par le quota."""
    now = now or datetime.now(timezone.utc)
    usage = usage or (lambda: usage_reel(settings))
    plan: list[dict[str, Any]] = []
    quota_cache: list[tuple[bool, str]] = []
    for obj in objectif.lister(settings, "ouvert"):
        session = obj.get("session")
        if not session:
            continue
        rc, _ = tmux(["has-session", "-t", f"={session}"])
        if rc == 127:
            continue  # tmux introuvable : on ne sait rien, on ne touche à rien
        if rc != 0:
            cause = "coupée"
        else:
            rc, ecran = tmux(["capture-pane", "-p", "-t", f"={session}:"])
            if rc != 0 or not MOTIFS_QUOTA.search(ecran):
                continue
            cause = "quota"
        entree: dict[str, Any] = {"id": obj["id"], "session": session, "cause": cause, "etape": obj["etape"],
                                  "commande": commande(obj), "action": "reprise", "raison": ""}
        if not quota_cache:
            try:
                quota_cache.append(_quota_revenu(usage()))
            except Exception as exc:  # noqa: BLE001 - usage illisible = pas de reprise
                quota_cache.append((False, f"usage illisible ({type(exc).__name__})"))
        ok, raison = quota_cache[0]
        raison = raison if not ok else _garde_fous(obj, now)
        if raison:
            entree.update(action="rien", raison=raison)
        elif not essai:
            _lancer(settings, obj, entree, lanceur, tmux)
        plan.append(entree)
    faits = [e for e in plan if e["action"] == "reprise"]
    verbe = "reprendrait" if essai else "reprise(s) lancée(s)"
    return {"essai": essai, "plan": plan,
            "resume": (f"{len(faits)} objectif(s) {verbe} sur {len(plan)} à reprendre" if plan
                       else "aucun objectif à reprendre")}


def _noms(tmux: Callable) -> set[str]:
    return {n for n in tmux(["list-sessions", "-F", "#{session_name}"])[1].split() if n}


def _lancer(settings: Settings, obj: dict[str, Any], entree: dict[str, Any], lanceur: Callable,
            tmux: Callable) -> None:
    cause = entree["cause"]
    avant = _noms(tmux)
    rc, sortie = lanceur(entree["commande"][1:])
    m = NOM_RE.search(sortie or "")
    nouvelle = m.group(1) if m else None
    if nouvelle is None:  # repli : la session apparue pendant l'appel
        nouvelle = next((n for n in sorted(_noms(tmux) - avant) if n.startswith("ultron-")), None)
    if nouvelle is None or not objectif.SESSION_RE.fullmatch(nouvelle):
        texte = f"{PREFIXE}échec du lancement ({cause}, rc {rc})"
        objectif._modifier(settings, obj["id"], lambda o: o["preuves"].append({"le": objectif._now(), "texte": texte}))
        entree.update(action="echec", raison=f"lancement sans nouvelle session (rc {rc})")
        return
    # Cause « quota » : l'ancienne session reste ouverte, elle est seulement déliée (lier remplace le nom).
    objectif.lier(settings, obj["id"], nouvelle)
    texte = f"{PREFIXE}{cause} -> {nouvelle}"
    objectif._modifier(settings, obj["id"], lambda o: o["preuves"].append({"le": objectif._now(), "texte": texte}))
    entree["nouvelle_session"] = nouvelle


def reprise_auto(settings: Settings) -> None:
    """Appel du heartbeat : une fois par passage, toute exception est journalisée sans casser le heartbeat."""
    try:
        res = reprendre(settings)
        if res["plan"]:
            print(f"objectif reprendre : {res['resume']}", file=sys.stderr)
    except BaseException as exc:  # noqa: BLE001
        print(f"objectif reprendre : {type(exc).__name__}: {exc}", file=sys.stderr)
