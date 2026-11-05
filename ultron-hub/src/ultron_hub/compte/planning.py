"""Daily plan of the accounts' points (todo #534, the user's option 1 of 05/11). No AI: pure functions + one small state file.

Parts, per account and per day: reserve (left to the user) = RESERVE_PCT % of the day's dose, at least MIN_RESERVE points;
auto = dose - reserve. The dose is `budget.dose_du_jour` taken at local midnight (it does not shrink while spending).
Fast end: within FAST_END of the account's weekly reset, its unused reserve goes to the auto (reserve 0).
The automatic switch (`auto.decide`) never goes to an account that spent its auto part, and stops when the active one did.

The « Planning du jour » box of the dashboard (`poser`) sets reserve A, reserve B (% of the dose) and the switch threshold
(% of the 5 h session); empty = defaults. It holds until midnight, Paris time, and always wins over what is learned.
The pause button (`pause`) stops the automatic switch until it is resumed.

Learning, per account, turns on by itself after LEARN_DAYS complete days of `compte releve` history (a complete day = readings
from 00:00 to 24:00 with no gap over `budget.SLEEP_AFTER`): an hour x weekday spending profile (exponential smoothing, ALPHA
for the new week) spreads the auto part over the day, and at each weekly reset a final % more than TOLERANCE points away from
the 100 % target moves the reserve by one STEP. Everything is recomputed from the history (35 days kept), nothing learned is
stored. The user's own measured share (90th percentile outside auto work) is left out: no signal tells who spent.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from ultron_hub.compte import budget, store
from ultron_hub.usage.collect import parse_time

PARIS = ZoneInfo("Europe/Paris")
RESERVE_PCT = 25
MIN_RESERVE = 3
THRESHOLD = 90  # % of the 5 h session of the active account
FAST_END = timedelta(hours=12)
LEARN_DAYS = 21  # three weeks of complete days
ALPHA = 0.3
STEP = 5
TOLERANCE = 5.0
RESERVE_MAX = 50
PCT_MAX = 100


def path(var_dir: Path) -> Path:
    return Path(var_dir) / "compte" / "planning.json"


def load(var_dir: Path) -> dict[str, Any]:
    """{planning: {...} | None, pause: {...} | None}; an unreadable file = nothing set."""
    try:
        data = json.loads(path(var_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {k: data.get(k) if isinstance(data.get(k), dict) else None for k in ("planning", "pause")}


def _save(var_dir: Path, data: dict[str, Any]) -> None:
    p = path(var_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".planning-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, p)


def paris_day(now: datetime) -> str:
    return now.astimezone(PARIS).date().isoformat()


def box_of_today(saved: dict[str, Any] | None, now: datetime) -> dict[str, Any] | None:
    """The « Planning du jour » box if it was set today (Paris), else None (expired at midnight or never set)."""
    return saved if saved and saved.get("jour") == paris_day(now) else None


def _check(value: int | None, low: int, high: int, what: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise store.CompteError(f"{what} refusé : entre {low} et {high}")
    return value


def poser(var_dir: Path, now: datetime, reserve_a: int | None = None, reserve_b: int | None = None,
          seuil: int | None = None, by: str = "cli") -> dict[str, Any]:
    """Set today's box (a field left empty = its default); all empty = the box is cleared."""
    values = {"reserve_a": _check(reserve_a, 0, PCT_MAX, "réserve A"), "reserve_b": _check(reserve_b, 0, PCT_MAX, "réserve B"),
              "seuil": _check(seuil, 1, PCT_MAX, "seuil")}
    data = load(var_dir)
    data["planning"] = ({"jour": paris_day(now), **values, "a": now.isoformat(), "by": by}
                        if any(v is not None for v in values.values()) else None)
    _save(var_dir, data)
    return data["planning"] or {}


def pause(var_dir: Path, now: datetime, on: bool, by: str = "cli") -> dict[str, Any]:
    data = load(var_dir)
    data["pause"] = {"a": now.isoformat(), "by": by} if on else None
    _save(var_dir, data)
    return {"pause": on, "a": now.isoformat()}


def threshold(box: dict[str, Any] | None) -> int:
    return box["seuil"] if box and box.get("seuil") is not None else THRESHOLD


# -- parts ---------------------------------------------------------------------------------------------------------------

def day_dose(acc: dict[str, Any], midnight: datetime) -> int | None:
    """The day's dose as it was at local midnight: today's spending is added back to the current %."""
    if not acc.get("connu") or acc.get("pourcent") is None:
        return None
    start = max(0.0, float(acc["pourcent"]) - float(acc.get("depense_aujourdhui") or 0))
    return budget.dose_du_jour(start, acc.get("remise_a_zero"), midnight)


def parts(dose: int | None, reserve_pct: int, hours_left: float | None) -> dict[str, Any] | None:
    """{dose, reserve, auto, fin_rapide}: reserve = reserve_pct % of the dose, at least MIN_RESERVE points (never more than
    the dose); in the last FAST_END before the reset the reserve goes to the auto."""
    if dose is None:
        return None
    fast = hours_left is not None and hours_left <= FAST_END.total_seconds() / 3600
    reserve = 0 if fast else min(dose, max(MIN_RESERVE, round(dose * reserve_pct / 100)))
    return {"dose": dose, "reserve": reserve, "auto": dose - reserve, "fin_rapide": fast}


# -- learning (pure, from the history) -----------------------------------------------------------------------------------

def _samples(history: Iterable[dict[str, Any]], slot: str) -> list[tuple[datetime, float]]:
    out = []
    for r in history:
        week = r.get("week")
        if r.get("compte") == slot and isinstance(week, dict) and isinstance(week.get("percent"), (int, float)):
            at = parse_time(r.get("at"))
            if at is not None:
                out.append((at, float(week["percent"])))
    return sorted(out)


def _midnight(d: date, zone: tzinfo) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=zone)


def complete_days(samples: list[tuple[datetime, float]], zone: tzinfo) -> list[date]:
    """Local days covered from 00:00 to 24:00 with no gap over budget.SLEEP_AFTER (a sleeping account leaves holes)."""
    gap = budget.SLEEP_AFTER
    days: dict[date, list[datetime]] = {}
    for at, _p in samples:
        days.setdefault(at.astimezone(zone).date(), []).append(at)
    out = []
    for d, ats in sorted(days.items()):
        start, end = _midnight(d, zone), _midnight(d + timedelta(days=1), zone)
        points = [start, *ats, end]
        if all(b - a <= gap for a, b in zip(points, points[1:])):
            out.append(d)
    return out


def hourly_spend(samples: list[tuple[datetime, float]], zone: tzinfo) -> dict[date, list[float]]:
    """Points spent per local hour (24 slots) and per day: rises of the %, a drop = a reset counted from zero."""
    out: dict[date, list[float]] = {}
    for (_a, prev), (at, p) in zip(samples, samples[1:]):
        local = at.astimezone(zone)
        out.setdefault(local.date(), [0.0] * 24)[local.hour] += p - prev if p >= prev else p
    return out


def profile(samples: list[tuple[datetime, float]], zone: tzinfo) -> dict[int, list[float]]:
    """{weekday: 24 shares summing to 1}, complete days only, exponential smoothing (ALPHA for the newer week)."""
    spend = hourly_spend(samples, zone)
    out: dict[int, list[float]] = {}
    for d in complete_days(samples, zone):
        hours = spend.get(d) or [0.0] * 24
        total = sum(hours)
        if total <= 0:
            continue
        share = [h / total for h in hours]
        old = out.get(d.weekday())
        out[d.weekday()] = share if old is None else [(1 - ALPHA) * o + ALPHA * s for o, s in zip(old, share)]
    return out


def resets(samples: list[tuple[datetime, float]]) -> list[dict[str, Any]]:
    """Weekly resets seen in the history: {a (first reading after), final (last % before)}."""
    return [{"a": at, "final": prev} for (_a, prev), (at, p) in zip(samples, samples[1:]) if p < prev]


def correction(final: float, reserve_pct: int) -> int:
    """Weekly correction: final % more than TOLERANCE under the target = the reserve was left unused, one STEP down;
    more than TOLERANCE over = one STEP up (the % stops at 100, so this side stays theoretical)."""
    gap = final - budget.TARGET
    step = -STEP if gap < -TOLERANCE else STEP if gap > TOLERANCE else 0
    return max(0, min(RESERVE_MAX, reserve_pct + step))


def learning(history: Iterable[dict[str, Any]], slot: str, zone: tzinfo = PARIS) -> dict[str, Any]:
    """{actif, jours_complets, reserve_pct, profil, bilans}. `bilans` = one line per weekly reset (planned 100 %, real final %,
    reserve before/after); a reset only corrects the reserve once LEARN_DAYS complete days came before it."""
    samples = _samples(history, slot)
    days = complete_days(samples, zone)
    reserve, bilans = RESERVE_PCT, []
    for r in resets(samples):
        before_days = sum(1 for d in days if d < r["a"].astimezone(zone).date())
        on = before_days >= LEARN_DAYS
        after = correction(r["final"], reserve) if on else reserve
        bilans.append({"compte": slot, "a": r["a"].isoformat(), "prevu": budget.TARGET, "reel": round(r["final"], 1),
                       "reserve_avant": reserve, "reserve_apres": after, "apprentissage": on})
        reserve = after
    on = len(days) >= LEARN_DAYS
    return {"actif": on, "jours_complets": len(days), "reserve_pct": reserve if on else RESERVE_PCT,
            "profil": profile(samples, zone) if on else None, "bilans": bilans}


def allowed_so_far(auto: int, shares: list[float] | None, local: datetime) -> float:
    """The auto part allowed by now: the whole part without a profile, else spread by the day's profile (hours before
    now plus the elapsed share of the current hour)."""
    if not shares:
        return float(auto)
    done = sum(shares[:local.hour]) + shares[local.hour] * local.minute / 60
    return round(auto * min(1.0, done), 1)


# -- the whole plan ------------------------------------------------------------------------------------------------------

def plan(figures: dict[str, Any], history: list[dict[str, Any]], saved: dict[str, Any], now: datetime,
         zone: tzinfo = PARIS) -> dict[str, Any]:
    """Pure. `figures` = `budget.budget(var_dir, now, PARIS)`, `history` = `budget.read_history`, `saved` = `load()`.
    Per account: dose, reserve, auto, spent today, auto allowed by now, `auto_epuisee` (None = unknown) and its text."""
    box = box_of_today(saved.get("planning"), now)
    local = now.astimezone(zone)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = []
    for acc in figures.get("comptes", []):
        slot = acc["nom"]
        learned = learning(history, slot, zone)
        boxed = box.get(f"reserve_{slot}") if box else None
        pct, source = ((boxed, "case") if boxed is not None
                       else (learned["reserve_pct"], "appris") if learned["actif"] else (RESERVE_PCT, "défaut"))
        p = parts(day_dose(acc, midnight), pct, acc.get("heures_restantes") if acc.get("remise_a_zero") else None)
        row: dict[str, Any] = {"nom": slot, "reserve_pct": pct, "source": source, "depense": acc.get("depense_aujourdhui"),
                               "apprentissage": learned["actif"], "jours_complets": learned["jours_complets"],
                               "remise_a_zero": acc.get("remise_a_zero"), "dose": None, "reserve": None, "auto": None,
                               "fin_rapide": False, "auto_permise": None, "auto_epuisee": None, "texte": None}
        if p:
            shares = (learned["profil"] or {}).get(local.weekday())
            allowed = allowed_so_far(p["auto"], shares, local)
            spent = float(acc.get("depense_aujourdhui") or 0)
            row.update(p, auto_permise=allowed, auto_epuisee=spent >= allowed,
                       texte=f"{slot.upper()} : {p['dose']} points = auto {p['auto']}, à toi {p['reserve']}"
                             + (" (fin rapide)" if p["fin_rapide"] else ""))
        rows.append(row)
    paused = saved.get("pause")
    return {"jour": paris_day(now), "case": box, "seuil": threshold(box), "seuil_source": "case" if box and box.get("seuil") is not None else "défaut",
            "pause": bool(paused), "pause_a": (paused or {}).get("a"), "comptes": rows,
            "texte": " · ".join(r["texte"] for r in rows if r["texte"]) or None}


def destinations(rows: list[dict[str, Any]], active: str | None) -> list[str]:
    """The other accounts, the one whose week resets soonest first (what is left there is lost at its reset)."""
    others = [r for r in rows if r["nom"] != active]
    return [r["nom"] for r in sorted(others, key=lambda r: (r.get("remise_a_zero") is None, r.get("remise_a_zero") or ""))]


def bilans_du_jour(history: list[dict[str, Any]], day: date, zone: tzinfo = PARIS) -> list[dict[str, Any]]:
    """The weekly-reset lines of a local day for the bilan du soir: planned, real (final %), reserve adjusted."""
    out = []
    for slot in store.SLOTS:
        for b in learning(history, slot, zone)["bilans"]:
            at = parse_time(b["a"])
            if at.astimezone(zone).date() != day:
                continue
            change = (f"réserve {b['reserve_avant']} % → {b['reserve_apres']} %" if b["apprentissage"]
                      else f"réserve {b['reserve_avant']} % (apprentissage pas encore allumé)")
            out.append({**b, "heure": at.astimezone(zone).strftime("%H:%M"),
                        "texte": f"Remise à zéro de {slot.upper()} : prévu {round(b['prevu'])} %, réel {round(b['reel'])} %, {change}"})
    return sorted(out, key=lambda x: x["a"])
