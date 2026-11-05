"""`chantier conseil` (todo #364): which session to launch now, by plain rules, WITHOUT AI. Read only.

Rules, in order: (a) a breakdown is going on (health alerts) -> Main; (b) a usage chantier (domaine `business`) has open
todos -> that chantier; (c) else the tooling chantier (any other domaine) with the most open todos. Paused chantiers (💤)
are never advised. Every source is best effort: one that cannot be read becomes a warning and the next rule applies.
The `bilan semaine` ratio is not used: it counts timer scripts versus AI jobs, not usage versus tooling.
"""

from __future__ import annotations

from typing import Any, Callable

from ultron_hub.chantier import core
from ultron_hub.common.settings import Settings

RAISON_MAX = 80
USAGE = "business"  # the `Domaine` of a usage chantier (FBA, Al Kitab, NovaPro...); everything else is tooling
PAUSE = "💤"
REGLES = {"panne": "a-panne", "usage": "b-usage", "outillage": "c-outillage", "rien": "d-rien"}


def _cut(text: str) -> str:
    text = " ".join(text.replace("—", ",").replace("–", ",").split())
    return text if len(text) <= RAISON_MAX else text[:RAISON_MAX - 1].rstrip() + "…"


def _seuls() -> set[str]:
    """Keys of the alerts whose reflex says `seul: true` (config/sante.yaml): fixed without anyone's gesture."""
    from ultron_hub.health import reflexes
    return {f"reflexe:{r['nom']}" for r in reflexes.charger() if r.get("seul")}


def _panne(settings: Settings, warnings: list[str]) -> str | None:
    """The first current health alert (var/health/alertes.json) needing someone's action, or None.

    An alert of a self-fixing reflex (todo #472) is only an info in the warnings, never a breakdown.
    """
    from ultron_hub.health import alertes
    try:
        actives = alertes.read_state(alertes.state_path(settings.root))
        seuls = _seuls() if actives else set()
    except Exception as exc:  # noqa: BLE001 - best effort
        warnings.append(f"santé illisible : {type(exc).__name__}")
        return None
    graves = []
    for key, text in actives.items():
        if key in seuls:
            warnings.append(f"alerte réglée seule : {text}")
        else:
            graves.append(text)
    return graves[0] if graves else None


def conseiller(settings: Settings, src: core.Sources, tableau: Callable[..., dict[str, Any]] = core.tableau,
               panne: Callable[[Settings, list[str]], str | None] = _panne) -> dict[str, Any]:
    warnings: list[str] = []
    out = {"chantier": "", "raison": "", "regle": REGLES["rien"], "avertissements": warnings}
    if text := panne(settings, warnings):
        out.update(raison=_cut(f"panne en cours : {text}"), regle=REGLES["panne"])
        return out
    try:
        data = tableau(settings, src)
        chantiers = [c for c in data["chantiers"] if c.get("todos") and c.get("groupe") != PAUSE]
        warnings.extend(data.get("avertissements") or [])
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"chantiers illisibles : {type(exc).__name__}")
        chantiers = []
    usage = [c for c in chantiers if c.get("domaine") == USAGE]
    if usage:
        best = max(usage, key=lambda c: len(c["todos"]))
        n = len(best["todos"])
        out.update(chantier=best["sujet"], regle=REGLES["usage"],
                   raison=_cut(f"{n} todo{'s' if n > 1 else ''} prête{'s' if n > 1 else ''}" + ", c'est de l'usage réel"))
        return out
    tools = [c for c in chantiers if c.get("domaine") != USAGE]
    if tools:
        best = max(tools, key=lambda c: len(c["todos"]))
        n = len(best["todos"])
        out.update(chantier=best["sujet"], regle=REGLES["outillage"],
                   raison=_cut(f"outillage : {n} todo{'s' if n > 1 else ''} prête{'s' if n > 1 else ''}"))
        return out
    out["raison"] = "aucune todo prête dans un chantier : session Main"
    return out
