"""Human-readable (French) renderings of collected metrics, shared by the CLI and the plugin."""

from typing import Any

STATES = {"charging": "en charge", "discharging": "sur batterie", "fully-charged": "pleine",
          "pending-charge": "charge en attente", "empty": "vide"}


def _fmt(value: float | None, unit: str = "", digits: int = 0) -> str:
    if value is None:
        return "?"
    return f"{value:.{digits}f}{unit}".replace(".", ",")


def duration_text(raw: str) -> str:
    """upower durations ("46.5 minutes", "1.2 hours") -> "47 min", "1 h 12"."""
    value, _, unit = raw.partition(" ")
    try:
        minutes = float(value) * (60 if unit.startswith("hour") else 1)
    except ValueError:
        return raw
    hours, mins = divmod(round(minutes), 60)
    return f"{hours} h {mins:02d}" if hours else f"{mins} min"


def battery_text(power: dict[str, Any] | None) -> str:
    if not power:
        return "🔋 Batterie : lecture impossible."
    ac = {True: "🔌 Secteur : branché", False: "⚠️ Secteur : DÉBRANCHÉ", None: "🔌 Secteur : inconnu"}
    lines = [ac[power.get("ac_online")]]
    bat = power.get("battery")
    if not bat:
        return "\n".join(lines + ["🔋 Aucune batterie détectée."])

    pct = bat.get("percentage")
    icon = "🪫" if pct is not None and pct < 20 else "🔋"
    lines.append(f"{icon} Batterie : {_fmt(pct, ' %')} ({STATES.get(bat.get('state'), bat.get('state') or '?')})")
    if bat.get("time_left"):
        kind = {"to_full": "pleine dans", "to_empty": "autonomie"}.get(bat.get("time_left_kind"), "temps")
        lines.append(f"⏱ {kind.capitalize()} : {duration_text(bat['time_left'])}")
    lines.append(f"❤️ Santé : {_fmt(bat.get('health_pct'), ' %')} "
                 f"({_fmt(bat.get('energy_full_wh'), ' Wh', 1)} / {_fmt(bat.get('energy_full_design_wh'), ' Wh', 1)})")
    if bat.get("charge_cycles") is not None:
        lines.append(f"🔁 Cycles : {bat['charge_cycles']}")
    if bat.get("temperature_c") is not None:
        lines.append(f"🌡 Température : {_fmt(bat['temperature_c'], ' °C', 1)}")
    return "\n".join(lines)
