"""Automatic account switch on a near quota (todo #534), called by the heartbeat. No AI, no signed token.

The rule is the written one of the switch alert (`alertes.switch_rule`, todo #329): the 5 h session of the active account
at or over the threshold of the day's plan (`planning`, 90 % by default, the « Planning du jour » box wins) while the other
has room. Refused (nothing switched, reason returned and logged): from a runner job or a squad (`ULTRON_HUB_JOB`,
`ULTRON_ESCOUADE_DIR`), automatic switch paused (dashboard button), active account over its auto part of the day (the auto
stops, the rest is the user's reserve), destination that spent its auto part or whose part is unknown, destination to log
into again (`a_reconnecter`, the OAuth login stays at the user's ✅), last switch less than MIN_GAP ago (re-checked under the
switch's lock, with the pause), destination itself over the threshold or unreadable.
A switch done here goes through `store.basculer` (same checks and writes as the signed one) with `by = "auto:quota"`;
its proof is kept in comptes.json (`derniere_bascule.by`, `bascules_auto`) and in the shared journal. The bilan du soir
lists it with a ↩️ that is the existing signed `compte-basculer` action.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ultron_hub.compte import alertes, budget, planning, store
from ultron_hub.usage import collect

log = logging.getLogger(__name__)

BY = "auto:quota"
MIN_GAP = timedelta(hours=6)
JOB_ENVS = ("ULTRON_HUB_JOB", "ULTRON_ESCOUADE_DIR")  # as auto_merge.JOB_ENVS
PAUSED = "bascule auto en pause (▶️ Reprendre au dashboard)"


def _recent(last: dict[str, Any] | None, now: datetime) -> bool:
    at = collect.parse_time((last or {}).get("a"))
    return at is not None and now - at < MIN_GAP


def _row(plan: dict[str, Any], slot: str) -> dict[str, Any] | None:
    return next((r for r in plan.get("comptes", []) if r["nom"] == slot), None)


def _parts_refusal(plan: dict[str, Any], active: str, dest: str) -> str:
    """Why the day's parts forbid the switch ("" = they allow it). Unknown parts refuse: the switch has no token."""
    act, other = _row(plan, active), _row(plan, dest)
    if act is None or act["auto_epuisee"] is None:
        return f"part auto du jour de {active.upper()} inconnue (pas de relevé)"
    if act["auto_epuisee"]:
        return (f"l'auto s'arrête : {active.upper()} a dépensé sa part auto du jour ({act['depense']} points pour "
                f"{act['auto_permise']} permis), le reste est ta réserve")
    if other is None or other["auto_epuisee"] is None:
        return f"part auto du jour de {dest.upper()} inconnue (pas de relevé)"
    if other["auto_epuisee"]:
        return (f"compte {dest.upper()} : part auto du jour déjà dépensée ({other['depense']} points pour "
                f"{other['auto_permise']} permis)")
    return ""


def decide(state: dict[str, Any], now: datetime, session_limit: float | None,
           week_limit: float | None, plan: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
    """(rule to apply, "") or (None, why not). Pure: `state` is `store.etat`'s output, `plan` is `planning.plan`'s
    (None = the day's parts and the pause are not checked)."""
    if plan and plan.get("pause"):
        return None, PAUSED
    rule = alertes.switch_rule(state, session_limit, week_limit)
    if rule is None:
        return None, "pas de limite posée" if session_limit is None else "quota du compte actif sous le seuil"
    dest = rule["vers"]
    if plan:
        # with two accounts the only destination is the other; the order (soonest reset first) matters beyond two
        if dest not in planning.destinations(plan.get("comptes", []), rule["de"])[:1]:
            return None, f"compte {dest.upper()} n'est pas le premier à se remettre à zéro"
        why = _parts_refusal(plan, rule["de"], dest)
        if why:
            return None, why
    if any(x.get("nom") == dest for x in state.get("a_reconnecter") or []):
        return None, f"compte {dest.upper()} à reconnecter (✅ au dashboard)"
    if _recent(state.get("derniere_bascule"), now):
        return None, f"dernière bascule il y a moins de {int(MIN_GAP.total_seconds() // 3600)} h"
    if rule["o_session"] is None:
        return None, f"quota de {dest.upper()} illisible"
    if not rule["room"]:
        return None, f"compte {dest.upper()} lui-même au-dessus du seuil"
    return rule, ""


def basculer_auto(settings: Any, now: datetime | None = None, *,
                  etat: Callable[..., dict[str, Any]] | None = None,
                  switch: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    """{fait: bool, raison: str, bascule: {...} | None}. Raises nothing it can catch (the heartbeat must go on)."""
    now = now or datetime.now(timezone.utc)
    if any(os.environ.get(k) for k in JOB_ENVS):
        return {"fait": False, "raison": "refusé depuis une tâche ou une escouade", "bascule": None}
    var_dir = Path(settings.var_dir)
    saved = planning.load(var_dir)
    if saved["pause"]:  # before any reading: paused, no network
        return {"fait": False, "raison": PAUSED, "bascule": None}
    cache_dir = var_dir / "cache"
    state = (etat or store.etat)(cache_dir, now)
    plan = planning.plan(budget.budget(var_dir, now, planning.PARIS), budget.read_history(var_dir), saved, now)
    rule, why = decide(state, now, plan["seuil"], alertes.WEEK_LIMIT, plan)
    if rule is None:
        return {"fait": False, "raison": why, "bascule": None}

    def authorize(target: str, _used) -> dict[str, Any]:
        # called under the switch's lock: a switch made meanwhile (button, other heartbeat) or a pause wins
        if target != rule["vers"] or _recent(store.load_meta()["derniere_bascule"], now):
            raise store.CompteError("une bascule vient d'être faite : rien n'a changé")
        if planning.load(var_dir)["pause"]:
            raise store.CompteError("bascule auto mise en pause : rien n'a changé")
        return {"by": BY}

    try:
        data = (switch or store.basculer)(cache_dir, vers=rule["vers"], authorize=authorize, now=now)
    except store.CompteError as exc:
        log.warning("compte bascule auto refused: %s", exc)
        return {"fait": False, "raison": str(exc), "bascule": None}
    dest = _row(plan, rule["vers"]) or {}
    regle = (f"session 5 h de {rule['de'].upper()} à {round(rule['session'])} % (seuil {round(plan['seuil'])} %), "
             f"{rule['vers'].upper()} à {round(rule['o_session'])} %, part auto de {rule['vers'].upper()} : "
             f"{dest.get('depense')} dépensés pour {dest.get('auto_permise')} permis")
    entry = {"a": store.now_iso(now), "de": data["de"], "vers": data["vers"], "by": BY, "regle": regle,
             "verifie": data["verification"]["ok"]}
    with store.locked():
        meta = store.load_meta()
        meta["bascules_auto"] = (meta["bascules_auto"] + [entry])[-store.AUTO_MAX:]
        store.save_meta(meta)
    log.info("compte basculer de=%s vers=%s by=%s verifie=%s", data["de"], data["vers"], BY, entry["verifie"])
    from ultron_hub.esprit import store as journal  # lazy: the journal never breaks the switch (record is best effort)
    journal.record(settings, "ultron-hub", "compte",
                   f"Bascule automatique de compte {data['de'].upper()} → {data['vers'].upper()} ({BY}) : {regle}. "
                   "↩️ au bilan du soir pour rebasculer.")
    return {"fait": True, "raison": regle, "bascule": entry}


def lignes_bilan(day: Any) -> list[dict[str, Any]]:
    """Lines of the bilan du soir (todo #530) for the automatic switches of a LOCAL day. A line is `annulee` once a later
    switch went back to its origin; its ↩️ is `compte-basculer` towards `retour`."""
    meta = store.load_meta()
    last = meta["derniere_bascule"] or {}
    out = []
    for b in meta["bascules_auto"]:
        at = collect.parse_time(b.get("a"))
        if at is None or at.astimezone().date() != day or b.get("de") not in store.SLOTS or b.get("vers") not in store.SLOTS:
            continue
        undone = str(last.get("a") or "") >= str(b["a"]) and last.get("vers") == b["de"]
        out.append({"id_evenement": f"compte-{b['a']}", "heure": at.astimezone().strftime("%H:%M"), "todo_id": None,
                    "titre": f"Bascule de compte {b['de'].upper()} → {b['vers'].upper()} ({b.get('regle') or 'quota'})",
                    "sorte": "bascule", "sha": None, "by": b.get("by") or BY, "annulee": undone, "retour": b["de"]})
    return out


def heartbeat(settings: Any) -> None:
    """Heartbeat call: once per beat, any error goes to stderr, never breaks the beat."""
    import sys
    try:
        res = basculer_auto(settings)
        if res["fait"]:
            print(f"compte bascule auto : {res['raison']}", file=sys.stderr)
    except BaseException as exc:  # noqa: BLE001
        print(f"compte bascule auto : {type(exc).__name__}: {exc}", file=sys.stderr)
