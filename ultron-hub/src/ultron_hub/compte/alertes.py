"""Account alerts (todo #329), checked at the end of each periodic reading (`budget.releve`). No network, no AI.

Three alerts, at most one per type and per half-day (local time, AM/PM), remembered in `var/compte/alertes.json`:
daily dose exceeded, and (only when the user sets explicit limits) 5 h session of the active account over `session_limit`
while the other has room (switch advice), weekly quota of the active account over `week_limit`. No default limit
(todo #424): without them only the dose alert exists.
"""

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ultron_hub import envoi
from ultron_hub.compte import budget, store

log = logging.getLogger(__name__)

SESSION_LIMIT: float | None = None  # optional, user-set; None = no alert
WEEK_LIMIT: float | None = None


def state_path(var_dir: Path) -> Path:
    return var_dir / "compte" / "alertes.json"


def _load(var_dir: Path) -> dict[str, str]:
    try:
        data = json.loads(state_path(var_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(data, dict) else {}


def _save(var_dir: Path, data: dict[str, str]) -> None:
    path = state_path(var_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".alertes-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def half_day(now: datetime) -> str:
    local = now.astimezone()
    return f"{local:%Y-%m-%d}-{'am' if local.hour < 12 else 'pm'}"


def _pct(window: Any) -> float | None:
    if isinstance(window, dict) and isinstance(window.get("percent"), (int, float)):
        return float(window["percent"])
    return None


def _fresh(row: dict[str, Any]) -> dict[str, Any] | None:
    quota = row.get("quota")
    return quota if row.get("enregistre") and quota and not quota.get("stale") else None


def switch_rule(state: dict[str, Any], session_limit: float | None, week_limit: float | None) -> dict[str, Any] | None:
    """The written switch rule (todo #329), shared by the alert and the automatic switch (todo #534).

    None unless `session_limit` is set and the fresh 5 h session of the active account is at or over it. Otherwise
    {de, vers, session, o_session, o_week, room}: `room` is true when the other account is registered with a fresh quota,
    its session under `session_limit` and its week under `week_limit` (when set)."""
    if session_limit is None:
        return None
    active_slot = state.get("actif")
    rows = {r["nom"]: r for r in state.get("comptes", [])}
    active = rows.get(active_slot) if active_slot else None
    quota = _fresh(active) if active else None
    session = _pct(quota.get("session")) if quota else None
    if session is None or session < session_limit:
        return None
    dest = store.other(active_slot)
    other_row = rows.get(dest)
    other_quota = _fresh(other_row) if other_row else None
    o_session = _pct(other_quota.get("session")) if other_quota else None
    o_week = _pct(other_quota.get("week")) if other_quota else None
    room = (o_session is not None and o_session < session_limit
            and (week_limit is None or o_week is None or o_week < week_limit))
    return {"de": active_slot, "vers": dest, "session": session, "o_session": o_session, "o_week": o_week, "room": room}


def candidates(state: dict[str, Any], figures: dict[str, Any], session_limit: float | None = None,
               week_limit: float | None = None) -> dict[str, str]:
    """{type: message} of the alerts that hold right now."""
    out: dict[str, str] = {}
    total = figures.get("total")
    if total:
        # The day's budget is left / days left, so it shrinks as we spend: compare with the budget at the start of the day.
        start = sum((r["restant"] + (r["depense_aujourdhui"] or 0)) / r["jours_restants"] for r in figures["comptes"] if r["connu"])
        if total["depense_aujourdhui"] > start > 0:
            out["budget"] = (f"Budget du jour dépassé : {total['depense_aujourdhui']} points dépensés depuis minuit "
                             f"pour {round(start, 1)} prévus (semaine).")
    active_slot = state.get("actif")
    rows = {r["nom"]: r for r in state.get("comptes", [])}
    active = rows.get(active_slot) if active_slot else None
    quota = _fresh(active) if active else None
    if not quota:
        return out
    names = {s: store.quel(s, {r["nom"]: r.get("adresse") for r in rows.values()}) for s in rows}
    week = _pct(quota.get("week"))
    rule = switch_rule(state, session_limit, week_limit)
    if rule and rule["room"]:
        o_week = rule["o_week"]
        out["bascule"] = (f"Bascule vers {names[rule['vers']]} maintenant, parce que la session de 5 h de "
                          f"{names[active_slot]} est à {round(rule['session'])} % alors que {names[rule['vers']]} est à "
                          f"{round(rule['o_session'])} % (semaine {round(o_week) if o_week is not None else '?'} %). "
                          "Bouton 🔁 Basculer au dashboard, onglet Moi.")
    if week_limit is not None and week is not None and week >= week_limit:
        out["semaine"] = (f"Semaine de {names[active_slot]} à {round(week)} % (limite {int(week_limit)} %) : "
                          "ménage ce compte jusqu'à sa remise à zéro.")
    return out


def verifier(var_dir: Path, state: dict[str, Any], now: datetime,
             send: Callable[..., Any] = envoi.envoyer) -> list[str]:
    """Send the alerts that hold and were not yet sent this half-day; return their types. Never raises."""
    try:
        todo = candidates(state, budget.budget(var_dir, now), SESSION_LIMIT, WEEK_LIMIT)
        if not todo:
            return []
        sent, key, done = _load(var_dir), half_day(now), []
        for kind, text in todo.items():
            if sent.get(kind) == key:
                continue
            result = send(text, sujet="alertes")
            if getattr(result, "status", "error") == "ok":  # an error is retried at the next reading
                sent[kind] = key
                done.append(kind)
        if done:
            _save(var_dir, sent)
        return done
    except Exception:  # the alerts must never break the reading
        log.exception("compte alertes failed")
        return []
