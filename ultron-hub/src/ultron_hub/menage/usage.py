"""What the user really uses on Telegram (todo #166): read the counter written by the plugin (`hermes_plugin/usage_log.py`).

`var/usage-reel/AAAA-MM.jsonl` = {"t": epoch, "k": key}. Read only, no AI. `DOORS` = the ways in that should be used:
the ⋯ Plus doors, the À toi action row and the three commands of the menu (a test keeps it in step with the plugin).
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

KEY_RE = re.compile(r"(?:hub:[a-z]{1,16}(?::[a-z]{1,16})?|cmd:[a-z][a-z0-9_]{0,31})")
DAYS = 30
DOORS: dict[str, str] = {
    "cmd:start": "/start (À toi)", "cmd:video": "/video", "cmd:plus": "/plus",
    "hub:todo:add": "➕ Ajouter", "hub:at:plus": "⋯ Plus",
    "hub:ch:list": "🏗 Chantiers", "hub:todo:list": "🎯 Toutes les todos",
    "hub:sante:menu": "🩺 Santé", "hub:usage:refresh": "📊 Usage", "hub:profil:show": "👤 Profil",
    "hub:an:open": "📥 Analyser", "hub:mtg:open": "🎙 Montage",
    "hub:mng:open": "🧹 Ménage", "hub:sv:ask": "🔎 Chercher", "hub:fo:open": "🧭 Focus", "hub:cap:open": "🔭 Cap",
    "hub:vl:ask": "🔔 Veille",
}


def folder(var_dir: Path) -> Path:
    return var_dir / "usage-reel"


def read(var_dir: Path, now: float | None = None, days: int = DAYS) -> Counter:
    """Keys pressed in the last `days` days. Unreadable lines and unknown shapes are skipped."""
    now = time.time() if now is None else now
    since = now - days * 86400
    counts: Counter = Counter()
    base = folder(var_dir)
    if not base.is_dir():
        return counts
    for path in sorted(base.glob("*.jsonl")):
        if path.is_symlink():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
                stamp, key = float(row["t"]), str(row["k"])
            except (ValueError, KeyError, TypeError):
                continue
            if stamp >= since and KEY_RE.fullmatch(key):
                counts[key] += 1
    return counts


def label(key: str) -> str:
    return DOORS.get(key, key)


def summary(var_dir: Path, now: float | None = None, days: int = DAYS) -> dict[str, Any]:
    counts = read(var_dir, now, days)
    started = folder(var_dir).is_dir() and any(folder(var_dir).glob("*.jsonl"))
    top = [{"cle": k, "libelle": label(k), "appuis": n} for k, n in counts.most_common(10)]
    never = [label(k) for k in DOORS if k not in counts]
    return {"jours": days, "total": sum(counts.values()), "top": top, "jamais": never, "compteur_actif": started}


def line(data: dict[str, Any]) -> str:
    """One line for the weekly tidy-up report."""
    if not data["compteur_actif"]:
        return "📊 Usage réel : compteur pas encore actif."
    top = ", ".join(f"{t['libelle']} {t['appuis']}" for t in data["top"][:3]) or "rien"
    never = ", ".join(data["jamais"][:6]) or "aucune"
    more = f" (+{len(data['jamais']) - 6})" if len(data["jamais"]) > 6 else ""
    return f"📊 Usage {data['jours']} j : {data['total']} appuis ; le plus : {top} ; jamais servi : {never}{more}"


def text(data: dict[str, Any]) -> str:
    if not data["compteur_actif"]:
        return "📊 Usage réel\nLe compteur n'a encore rien enregistré (il démarre après la fusion de la todo #166)."
    lines = [f"📊 Usage réel ({data['jours']} derniers jours) : {data['total']} appuis"]
    lines += [f"• {t['libelle']} : {t['appuis']}" for t in data["top"]]
    lines += ["", "Jamais servi : " + (", ".join(data["jamais"]) or "aucune porte")]
    lines.append("Candidats au retrait : à décider par toi, rien n'est retiré tout seul.")
    return "\n".join(lines)
