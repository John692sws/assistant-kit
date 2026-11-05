"""Thresholds of the internal watcher, read from config/sante.yaml (never hard-coded, todos #159 and #160).

A missing or unreadable file, or a bad value, falls back to the defaults below: the watcher never stops because of it.
"""

from pathlib import Path
from typing import Any

import yaml

from ultron_hub.common.settings import project_root

DEFAUTS: dict[str, dict[str, float]] = {
    "pouls": {"collecte_max_h": 2},
    "alertes": {"disque_pct": 85, "sauvegarde_max_h": 30, "check_max_jours": 8, "restauration_max_jours": 35,
                "depot_max_h": 48},
}


def config_path(root: Path | None = None) -> Path:
    return (root or project_root()) / "config" / "sante.yaml"


def charger(path: Path | None = None) -> dict[str, dict[str, float]]:
    out = {section: dict(values) for section, values in DEFAUTS.items()}
    try:
        data: Any = yaml.safe_load((path or config_path()).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return out
    if not isinstance(data, dict):
        return out
    for section, values in out.items():
        given = data.get(section)
        if not isinstance(given, dict):
            continue
        for key in values:
            v = given.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                values[key] = v
    return out
