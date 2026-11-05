"""The blocks of the page: JSON built from the existing commands' Python functions (no business rule here).

Only whitelisted fields leave this module: nothing like a token, a key path or a work folder is ever shown.
"""

import argparse
import re
import threading
import time
from typing import Any, Callable

from ultron_hub.chantier import commands as chantier_commands
from ultron_hub.chantier import core as chantier_core
from ultron_hub.common.settings import load_settings
from ultron_hub.dashboard import actions, catalogue
from ultron_hub.escouade import core as escouade_core
from ultron_hub.health import collect as health_collect
from ultron_hub.usage import commands as usage_commands

CACHE_S = 15.0  # one refresh per phone every 30 s; the quota itself is cached 60 s by `usage`
TEXT_MAX = 300
SUJET_RE = escouade_core.NAME_RE
_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()


def _short(value: Any, limit: int = TEXT_MAX) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _ns(**kw: Any) -> argparse.Namespace:
    return argparse.Namespace(dossier=None, boites_dir=None, **kw)


def _ok(envelope: dict[str, Any]) -> dict[str, Any]:
    if envelope.get("status") != "ok":
        raise RuntimeError(envelope.get("error") or "command failed")
    return envelope.get("data") or {}


def _window(w: Any) -> dict[str, Any] | None:
    if not isinstance(w, dict):
        return None
    return {"percent": w.get("percent"), "remise_a_zero": w.get("resets_at")}


def quota() -> dict[str, Any]:
    env = usage_commands.handle(argparse.Namespace(), load_settings())
    if env.get("status") != "ok":
        raise RuntimeError("usage indisponible")
    data = env.get("data") or {}
    return {"session": _window(data.get("session")), "semaine": _window(data.get("week")), "cache": bool(data.get("stale"))}


def phrase_a_toi(items: list[dict[str, Any]]) -> str:
    """Ultron's one sentence about what waits for the user (todo #345); computed here, the page decides nothing."""
    branches = sum(1 for it in items if it.get("kind") == "fusionner")
    questions = sum(1 for it in items if it.get("question"))
    autres = len(items) - branches - questions
    parts = []
    if branches:
        parts.append(f"ton feu vert sur {branches} branche{'s' if branches > 1 else ''}")
    if questions:
        parts.append(f"ta réponse à {questions} question{'s' if questions > 1 else ''}")
    if autres:
        parts.append(f"ton avis sur {autres} autre{'s' if autres > 1 else ''} point{'s' if autres > 1 else ''}")
    if not parts:
        return "Rien ne t'attend."
    return "Il me faut " + (parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " et " + parts[-1]) + "."


def a_toi() -> dict[str, Any]:
    data = _ok(chantier_commands.handle(_ns(chantier_command="atoi", chantier=None), load_settings()))
    items = [{"icone": it.get("icone", ""), "kind": it.get("kind"), "chantier": it.get("chantier"),
              "ref": f"#{it['todo']}" if it.get("todo") else (it.get("question") or ""), "texte": _short(it.get("texte")),
              "bloquante": bool(it.get("bloquante")), "todo": it.get("todo") or None,
              "question": it.get("question") or None, **catalogue.decision(it.get("kind"))} for it in data.get("items", [])]
    return {"items": items, "total": len(items), "phrase": phrase_a_toi(items)}


def sante() -> dict[str, Any]:
    m = health_collect.collect()
    disks = m.get("disks") or []
    cpus = (m.get("system") or {}).get("cpus") or 1
    user = (m.get("services") or {}).get("user") or {}
    return {"disque_pct": max((d.get("used_pct") or 0 for d in disks), default=None),
            "ram_pct": (m.get("memory") or {}).get("used_pct"),
            "charge": (m.get("system") or {}).get("load_1m"), "cpus": cpus,
            "hermes_gateway": user.get("hermes-gateway"),
            "unites_en_echec": sorted(((m.get("services") or {}).get("failed_user") or [])
                                      + ((m.get("services") or {}).get("failed_system") or []))[:10],
            "sauvegarde_age_h": (m.get("backup") or {}).get("last_success_age_h")}


def chantiers() -> dict[str, Any]:
    settings = load_settings()
    data = _ok(chantier_commands.handle(_ns(chantier_command="tableau"), settings))
    faites = chantier_core.done_counts(settings)  # todo #281: x/y todos on the home screen
    return {"chantiers": [{"sujet": c["sujet"], "titre": _short(c.get("titre"), 80), "groupe": c.get("groupe"),
                           "hors": bool(c.get("hors")), "ouvertes": len(c.get("todos") or []), "faites": faites.get(c["sujet"], 0),
                           "a_toi": len(c.get("a_toi") or [])}
                          for c in data.get("chantiers", [])]}


def fiche(sujet: str) -> dict[str, Any]:
    if sujet != "-" and not SUJET_RE.fullmatch(sujet):
        raise ValueError("nom de chantier refusé")
    d = _ok(chantier_commands.handle(_ns(chantier_command="fiche", sujet=sujet), load_settings()))
    return {"sujet": d.get("sujet"), "titre": _short(d.get("titre"), 80), "groupe": d.get("groupe"),
            "ou_en_est": _short(d.get("ou_en_est"), 600), "reprise": [_short(x, 200) for x in (d.get("reprise") or [])][:12],
            "todos": [{"id": t.get("id"), "titre": _short(t.get("titre"), 80)} for t in d.get("todos", [])][:20],
            "questions": [{"id": q.get("id"), "texte": _short(q.get("texte"))} for q in d.get("questions", [])][:10]}


BLOCKS: dict[str, Callable[[], dict[str, Any]]] = {
    "quota": quota, "a_toi": a_toi, "sante": sante, "chantiers": chantiers}


def block(name: str) -> dict[str, Any]:
    """One block, cached CACHE_S seconds; an error stays inside the block (`{"erreur": ...}`)."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(name)
        if hit and now - hit[0] < CACHE_S:
            return hit[1]
    try:
        value = {"ok": True, **BLOCKS[name]()}
    except Exception as exc:  # noqa: BLE001 - the page keeps the other blocks
        value = {"ok": False, "erreur": type(exc).__name__}
    with _lock:
        _cache[name] = (now, value)
    return value


def state() -> dict[str, Any]:
    return {"genere_a": time.strftime("%H:%M:%S"), **{name: block(name) for name in BLOCKS}}


def clear_cache() -> None:
    with _lock:
        _cache.clear()
