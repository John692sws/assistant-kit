"""Daily budget of the two Claude accounts (todo #328): how many weekly points to spend today, a plain script.

`releve` (network, run every 15 min by a systemd user timer) appends the quota of each account to
`var/compte/historique.jsonl`; `budget` (files only, no network) computes the figures from that history.
The history holds percentages and dates only, never a token. A sleeping account (token expired, never refreshed here)
keeps its last known value and the time it was read.
"""

import json
import math
import os
import tempfile
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from ultron_hub.compte import store
from ultron_hub.usage.collect import parse_time

TARGET = 100.0  # the only goal: each account fully spent at ITS OWN weekly reset (no default threshold, todo #424)
LIMIT = TARGET  # kept as the name of the target for `restant`
KEEP = timedelta(days=35)
SLEEP_AFTER = timedelta(minutes=45)  # three missed readings: the account is asleep
WEEK_DAYS = 7


def dose_du_jour(percent: float | None, resets_at: Any, now: datetime) -> int | None:
    """Points to spend today to reach 100 % at the weekly reset of THIS account: (100 - percent) / days left, rounded.
    Days left is decimal with a floor of 1 day (no absurd dose in the last hours); no reset known = a full week."""
    if percent is None:
        return None
    resets = parse_time(resets_at) if resets_at else None
    days = max(1.0, (resets - now).total_seconds() / 86400) if resets and resets > now else (1.0 if resets else float(WEEK_DAYS))
    return round(max(0.0, TARGET - float(percent)) / days)


def dose_texte(doses: dict[str, int | None]) -> str | None:
    """« Aujourd'hui : A 12 points, B 9 points » (accounts with a known dose only)."""
    parts = [f"{slot.upper()} {dose} points" for slot, dose in doses.items() if dose is not None]
    return "Aujourd'hui : " + ", ".join(parts) if parts else None


def history_path(var_dir: Path) -> Path:
    return var_dir / "compte" / "historique.jsonl"


def read_history(var_dir: Path) -> list[dict[str, Any]]:
    rows = []
    try:
        lines = history_path(var_dir).read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("compte") in store.SLOTS and parse_time(row.get("at")) is not None:
            rows.append(row)
    return rows


def _write_history(var_dir: Path, rows: list[dict[str, Any]]) -> None:
    path = history_path(var_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".historique-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
    os.replace(tmp, path)


def releve(var_dir: Path, cache_dir: Path, now: datetime) -> dict[str, Any]:
    """Read the quota of each registered account (existing code) and append the fresh values to the history."""
    state = store.etat(cache_dir, now)
    added, skipped = [], []
    new = []
    for row in state["comptes"]:
        quota = row.get("quota")
        if not row["enregistre"] or not quota or quota.get("stale") or not quota.get("week"):
            skipped.append(row["nom"])
            continue
        new.append({"at": now.isoformat(), "compte": row["nom"], "session": quota.get("session"), "week": quota["week"]})
        added.append(row["nom"])
    if new:
        old = [r for r in read_history(var_dir) if now - parse_time(r["at"]) <= KEEP]
        _write_history(var_dir, old + new)
    from ultron_hub.compte import alertes  # late: alertes imports this module

    return {"releves": added, "ignores": skipped, "alertes": alertes.verifier(var_dir, state, now)}


def _spent_today(samples: list[dict[str, Any]], midnight: datetime) -> float | None:
    """Weekly points used since midnight: increments of the percentage, a drop meaning a reset (counted from zero)."""
    before = [s for s in samples if parse_time(s["at"]) < midnight]
    today = [s for s in samples if parse_time(s["at"]) >= midnight]
    if not today:
        return None if not before else 0.0
    prev = (before[-1] if before else today[0])["week"]["percent"]
    spent = 0.0
    for s in today:
        p = s["week"]["percent"]
        spent += p - prev if p >= prev else p
        prev = p
    return round(spent, 1)


def _account(slot: str, samples: list[dict[str, Any]], now: datetime, midnight: datetime, zone: tzinfo) -> dict[str, Any]:
    out: dict[str, Any] = {"nom": slot, "connu": False, "veille": False, "lu_a": None, "pourcent": None, "restant": None,
                           "remise_a_zero": None, "heures_restantes": None, "jours_restants": None, "budget_jour": None,
                           "depense_aujourdhui": None, "remise_passee": False, "dose": None}
    if not samples:
        return out
    last = samples[-1]
    read_at = parse_time(last["at"])
    percent = float(last["week"]["percent"])
    resets = parse_time(last["week"].get("resets_at"))
    out.update(connu=True, lu_a=read_at.astimezone(zone).isoformat(), veille=now - read_at > SLEEP_AFTER)
    if resets is not None and resets <= now:
        # The week turned over while the account slept: nothing was used since, so it starts again at zero.
        out["remise_passee"] = True
        percent, resets = 0.0, None
    remaining = max(0.0, LIMIT - percent)
    hours = (resets - now).total_seconds() / 3600 if resets else WEEK_DAYS * 24.0
    days = max(1, math.ceil(hours / 24))
    out.update(pourcent=round(percent, 1), restant=round(remaining, 1), remise_a_zero=resets.isoformat() if resets else None,
               heures_restantes=round(hours, 1), jours_restants=days, budget_jour=round(remaining / days, 1),
               depense_aujourdhui=_spent_today(samples, midnight), dose=dose_du_jour(percent, resets.isoformat() if resets else None, now))
    return out


def budget(var_dir: Path, now: datetime, zone: tzinfo | None = None) -> dict[str, Any]:
    """Per account: points left up to 100 %, time before the weekly reset, budget of the day (left / days left), points
    used since local midnight, last known value if asleep. Plus the total and the advice. Files only."""
    zone = zone or now.astimezone().tzinfo
    local = now.astimezone(zone)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    history = read_history(var_dir)
    try:
        names = store.adresses_of(store.load_meta())
    except Exception:  # the address is cosmetic: never an error
        names = {}
    rows = []
    for slot in store.SLOTS:
        samples = sorted((r for r in history if r["compte"] == slot and isinstance(r.get("week"), dict)
                          and isinstance(r["week"].get("percent"), (int, float))), key=lambda r: r["at"])
        acc = _account(slot, samples, now, midnight, zone)
        acc["adresse"] = names.get(slot)
        rows.append(acc)
    known = [r for r in rows if r["connu"]]
    total = {
        "restant": round(sum(r["restant"] for r in known), 1),
        "budget_jour": round(sum(r["budget_jour"] for r in known), 1),
        "depense_aujourdhui": round(sum(r["depense_aujourdhui"] or 0 for r in known), 1),
    }
    dose = dose_texte({r["nom"]: r["dose"] for r in known})
    return {"limite": LIMIT, "at": now.isoformat(), "comptes": rows, "total": total if known else None,
            "dose_texte": dose, "conseil": _advice(known)}


def _advice(known: list[dict[str, Any]]) -> str | None:
    """Spend first the account whose week resets soonest: what is left there is lost at the reset."""
    usable = [r for r in known if r["restant"] and r["remise_a_zero"]]
    if len(known) < 2 or not usable:
        return None
    first = min(usable, key=lambda r: r["heures_restantes"])
    nom = first.get("adresse") or f"le compte {first['nom'].upper()}"  # todo #413: the address, as everywhere else
    return f"Épuise d'abord {nom} : sa semaine se remet à zéro dans {_duration(first['heures_restantes'])}."


def _duration(hours: float) -> str:
    return f"{round(hours)} h" if hours < 48 else f"{round(hours / 24)} j"
