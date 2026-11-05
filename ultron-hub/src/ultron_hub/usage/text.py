"""Telegram text for /usage: two lines, readable at a glance."""

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ultron_hub.usage.collect import parse_time

PARIS = ZoneInfo("Europe/Paris")
DAYS = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")
BAR_CELLS = 10


def bar(percent: float) -> str:
    full = min(BAR_CELLS, max(0, round(percent / 100 * BAR_CELLS)))
    return "▓" * full + "░" * (BAR_CELLS - full)


def _icon(percent: float) -> str:
    return "🟢" if percent < 50 else "🟡" if percent < 80 else "🔴"


def _reset(value: str | None, now: datetime) -> str:
    dt = parse_time(value)
    if dt is None:
        return "réinitialisation inconnue"
    # The endpoint returns e.g. 00:59:59.98 for a 01:00 reset: show the nearest minute.
    local = (dt + timedelta(seconds=30)).replace(second=0, microsecond=0).astimezone(PARIS)
    today = now.astimezone(PARIS).date()
    if local.date() == today:
        return f"repart à zéro à {local:%Hh%M}"
    return f"repart à zéro {DAYS[local.weekday()]} {local:%d/%m} à {local:%Hh%M}"


def _line(label: str, window: dict | None, now: datetime) -> str:
    if window is None:
        return f"⚪ {label} : non disponible"
    p = window["percent"]
    return f"{_icon(p)} {label} : {p:g} %  {bar(p)}\n      {_reset(window['resets_at'], now)}"


def usage_text(result: dict[str, Any], now: datetime) -> str:
    lines = ["📊 Usage Claude (abonnement)", "",
             _line("Session 5 h", result.get("session"), now),
             _line("Semaine", result.get("week"), now)]
    if result.get("stale"):
        fetched = parse_time(result.get("fetched_at"))
        when = f" de {fetched.astimezone(PARIS):%Hh%M}" if fetched else ""
        lines += ["", f"⚠️ Valeurs en cache{when} (trop de requêtes)."]
    return "\n".join(lines)


def summary_line(result: dict[str, Any]) -> str:
    parts = [f"{label} {w['percent']:g} %" for label, w in (("session", result.get("session")),
                                                            ("semaine", result.get("week"))) if w]
    return "usage : " + ", ".join(parts)
