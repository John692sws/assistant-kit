"""`bilan semaine` (todo #256): what the last 7 days really were, counted WITHOUT AI.

Four counters: todos delivered or closed by nature, Telegram buttons used (usage counter), timer runs (scripts) versus
`claude -p` jobs (AI), week quota share. Every source is best effort: an unreadable one is listed in `warnings`.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ultron_hub.cap.collect import _ts
from ultron_hub.menage import usage as button_usage

DAYS = 7
STARTED = re.compile(r"Start(?:ing|ed) (ultron-hub-[A-Za-z0-9_.@-]+?)\.service")


def journal_lines(days: int = DAYS) -> list[str]:
    """User journal of the last `days` days (journalctl, message only)."""
    out = subprocess.run(["journalctl", "--user", "--since", f"{days} days ago", "-o", "cat", "--no-pager"],
                         capture_output=True, text=True, timeout=60, check=True)
    return out.stdout.splitlines()


def todos(db: Any, since: float, warnings: list[str]) -> dict[str, Any]:
    """Todos closed (done) or delivered (open, last event `livree`) since `since`, by `nature` (no usage marker exists)."""
    empty = {"fond": 0, "chantier": 0, "total": 0, "ids": {"fond": [], "chantier": []}}
    if db is None or not Path(db).is_file():
        warnings.append("base des todos introuvable")
        return empty
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
        try:
            rows = conn.execute("SELECT id, nature, status, done_at FROM tasks").fetchall()
            last: dict[int, tuple[str, str, str]] = {}
            for tid, at, action, details in conn.execute("SELECT task_id, at, action, details FROM events ORDER BY id"):
                try:
                    keys = set(json.loads(details))
                except (TypeError, ValueError):
                    keys = set()
                if action == "edit" and keys <= {"clair"}:
                    continue  # filled in by the digest, not by a person (same rule as is_delivered)
                last[tid] = (action, at)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        warnings.append(f"todos illisibles : {type(exc).__name__}")
        return empty
    ids: dict[str, list[int]] = {"fond": [], "chantier": []}
    for tid, nature, status, done_at in rows:
        if status == "done":
            hit = (_ts(done_at) or 0) >= since
        else:
            event = last.get(tid)
            hit = status == "todo" and event is not None and event[0] == "livree" and (_ts(event[1]) or 0) >= since
        if hit:
            ids["fond" if nature == "fond" else "chantier"].append(tid)
    return {"fond": len(ids["fond"]), "chantier": len(ids["chantier"]),
            "total": len(ids["fond"]) + len(ids["chantier"]), "ids": ids}


def buttons(var_dir: Path, now: float) -> dict[str, Any]:
    """Buttons pressed, grouped by action (`hub:<module>` or `cmd:<name>`), and the share of merges (`hub:merge`)."""
    raw = button_usage.read(var_dir, now=now, days=DAYS)
    by_action: Counter = Counter()
    for key, n in raw.items():
        by_action[":".join(key.split(":")[:2]) if key.startswith("hub:") else key] += n
    total = sum(by_action.values())
    merges = by_action.get("hub:merge", 0)
    return {"total": total, "par_action": dict(by_action.most_common(10)), "fusions": merges,
            "part_fusions_pct": round(100 * merges / total) if total else None}


def scripts_vs_ai(runs_dir: Path, since: float, warnings: list[str],
                  lines: Callable[[], list[str]] = journal_lines) -> dict[str, Any]:
    """Timer runs (scripts, from the journal) versus `claude -p` jobs (var/runs/*/request.json, by action)."""
    timers: Counter = Counter()
    try:
        for line in lines():
            found = STARTED.search(line)
            if found:
                timers[found.group(1)] += 1
    except Exception as exc:  # noqa: BLE001 - no journal access must not hide the other counters
        warnings.append(f"journal illisible : {type(exc).__name__}")
    ai: Counter = Counter()
    try:
        for path in Path(runs_dir).glob("*/request.json"):
            try:
                if path.is_symlink() or path.stat().st_mtime < since:
                    continue
                ai[str(json.loads(path.read_text(encoding="utf-8")).get("action") or "?")] += 1
            except (OSError, ValueError, AttributeError):
                continue
    except OSError as exc:
        warnings.append(f"jobs IA illisibles : {type(exc).__name__}")
    n_scripts, n_ai = sum(timers.values()), sum(ai.values())
    return {"minuteries": n_scripts, "ia": n_ai, "par_minuterie": dict(timers.most_common(10)),
            "par_action_ia": dict(ai.most_common(10)),
            "part_scripts_pct": round(100 * n_scripts / (n_scripts + n_ai)) if n_scripts + n_ai else None}


def build_text(d: dict[str, Any]) -> str:
    t, b, r = d["todos"], d["boutons"], d["ratio"]
    out = ["📊 Bilan de la semaine",
           f"Todos finies : {t['total']} ({t['fond']} de fond, {t['chantier']} de chantier)."]
    if b["total"]:
        out.append(f"Boutons : {b['total']} appuis, {b['part_fusions_pct']} % de fusions.")
    else:
        out.append("Boutons : aucun appui compté.")
    if r["part_scripts_pct"] is None:
        out.append("Scripts contre IA : rien de compté.")
    else:
        out.append(f"Scripts : {r['minuteries']} lancements de minuteries. IA : {r['ia']} jobs. "
                   f"{r['part_scripts_pct']} % de scripts.")
    pct = d["quota_semaine_pct"]
    out.append(f"Quota de la semaine : {round(pct)} %." if pct is not None else "Quota de la semaine : non mesuré.")
    if d["warnings"]:
        out.append(f"Sources manquantes : {len(d['warnings'])}.")
    return "\n".join(out)


def run(settings: Any, now: float | None = None, lines: Callable[[], list[str]] = journal_lines,
        quota: Callable[[Any], float | None] | None = None) -> dict[str, Any]:
    """The weekly count. Never raises, never calls an AI."""
    from ultron_hub.cap import report
    now = time.time() if now is None else now
    since = now - DAYS * 86400
    warnings: list[str] = []
    data: dict[str, Any] = {
        "depuis": datetime.fromtimestamp(since).astimezone().isoformat(timespec="seconds"),
        "jusqu_a": datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
        "todos": todos(settings.todo_db, since, warnings),
        "todos_note": "nature fond|chantier de la base (aucun marqueur d'usage réel sur les todos)",
        "boutons": buttons(Path(settings.var_dir), now),
        "ratio": scripts_vs_ai(Path(settings.runs_dir), since, warnings, lines),
        "quota_semaine_pct": (quota or report.week_percent)(settings),
    }
    data["warnings"] = warnings
    data["text"] = build_text(data)
    return data


def send(settings: Any, data: dict[str, Any], sender: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Send the text through the common point (`todo.service.send_message`). A failed send is reported, not fatal."""
    from ultron_hub.todo import service
    sender = sender or service.send_message
    try:
        sender(data["text"], settings.todo_notify_target, settings.hermes_binary, sujet="revue-de-cap")
        return {"sent": True, "send_error": None}
    except Exception as exc:  # noqa: BLE001
        return {"sent": False, "send_error": f"{type(exc).__name__}: {str(exc)[:160]}"}
