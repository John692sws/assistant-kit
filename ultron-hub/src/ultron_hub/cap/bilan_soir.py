"""`bilan soir [--date AAAA-MM-JJ]` (todo #530): what was done ALONE in a day, read only, without AI.

Source: the table `events` of the todo store. An event counts when `details.by` starts with `auto:` (the orchestrator's
`branch auto-merge` writes `livree` by `auto:orchestrateur`, the note holding the sha; the future `todo close-auto`
(todo #528) writes a `status` to done by `auto:...`). One line per event: {id_evenement, heure, todo_id, titre, sorte
(fusion | clôture), sha, annulee}. `id_evenement` is the key the ↩️ button (todo #531) and the counter (todo #532) plug into.
Todo #534: the automatic account switches of the day (`compte/auto.lignes_bilan`, from comptes.json) are added as
`sorte` « bascule » with `retour` (the slot the ↩️ `compte-basculer` goes back to), and `remises` = one line per weekly
reset of an account that day (planned, real final %, reserve adjusted: `compte/planning.bilans_du_jour`, from the history).
Todo #535: a todo cancelled by `auto:bilan` (ménage proposé or Sunday lot applied alone, `menage_lot.appliquer_auto`) is a
line of sorte `ménage` or `rapports`; what the same applications did outside the todos (chantier archived, skill or folder
moved to a `.trash`) is read from var/menage/propositions-appliquees.jsonl and var/rapports/appliques.jsonl (`by` =
`auto:bilan`), one line each with `id_evenement` "menage:<heure>" / "rapports:<heure>", `todo_id` null; ↩️ = put it back
from the `.trash` (or reopen the todo).
The day is the LOCAL day of the server. `annulee` is true when a later `fusion-annulee` or a `reopen` of that todo exists.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.settings import Settings

AUTO_PREFIX = "auto:"
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
SHA_RE = re.compile(r"\b[0-9a-f]{12}\b")


def _local(at: str) -> datetime:
    return datetime.fromisoformat(at).astimezone()


def _hors_todos(settings: Settings, target: date) -> list[dict[str, Any]]:
    """Todo #535: the unattended applications of the day that left no todo event to list (see the module docstring)."""
    out = []
    for sorte, rel in (("ménage", "menage/propositions-appliquees.jsonl"), ("rapports", "rapports/appliques.jsonl")):
        try:
            records = (settings.var_dir / rel).read_text(encoding="utf-8").splitlines()
        except (OSError, AttributeError):
            continue
        for line in records:
            try:
                rec = json.loads(line)
                when = _local(rec["at"])
            except (ValueError, KeyError, TypeError):
                continue
            if not isinstance(rec, dict) or rec.get("by") != "auto:bilan" or when.date() != target:
                continue
            if sorte == "ménage":
                faits = [f for f in rec.get("faits") or [] if not str(f).startswith("#")]
                titre = "Ménage appliqué seul : " + ", ".join(faits)
            else:
                faits = [a.get("texte", "") for a in rec.get("actions") or [] if a.get("type") == "todo"]
                titre = "Rapports appliqués seuls : " + ", ".join(faits)
            if not faits:
                continue
            out.append({"id_evenement": f"{sorte}:{when.strftime('%H:%M:%S')}", "heure": when.strftime("%H:%M"),
                        "todo_id": None, "titre": titre[:300], "sorte": sorte, "sha": None, "by": "auto:bilan",
                        "annulee": False})
    return out


def run(settings: Settings, day: str | None = None) -> dict[str, Any]:
    if day is not None and not DATE_RE.fullmatch(day):
        raise InvalidRequestError("--date must be AAAA-MM-JJ")
    try:
        target = date.fromisoformat(day) if day else datetime.now().astimezone().date()
    except ValueError as exc:
        raise InvalidRequestError(f"invalid date: {day}") from exc
    conn = sqlite3.connect(f"file:{settings.todo_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # a margin of one day each side, then the exact local day is kept below
        lo, hi = (target - timedelta(days=1)).isoformat(), (target + timedelta(days=2)).isoformat()
        rows = conn.execute(
            "SELECT e.id, e.task_id, e.at, e.action, e.details, t.title FROM events e JOIN tasks t ON t.id = e.task_id "
            "WHERE e.at >= ? AND e.at < ? ORDER BY e.id", (lo, hi)).fetchall()
        later = {}
        for r in conn.execute("SELECT id, task_id, action FROM events WHERE action IN ('fusion-annulee', 'reopen') "
                              "OR (action = 'status' AND details LIKE '%\"to\": \"todo\"%')"):
            later.setdefault(r["task_id"], []).append((r["id"], r["action"]))
    finally:
        conn.close()
    lignes = []
    for r in rows:
        try:
            details = json.loads(r["details"])
        except ValueError:
            continue
        by = details.get("by") if isinstance(details, dict) else None
        if not isinstance(by, str) or not by.startswith(AUTO_PREFIX):
            continue
        if r["action"] == "livree":
            sorte = "fusion"
        elif r["action"] == "status" and details.get("to") == "done":
            sorte = "clôture"
        elif r["action"] == "status" and details.get("to") == "cancelled" and by == "auto:bilan":
            sorte = "rapports" if details.get("rapports") else "ménage"
        else:
            continue
        when = _local(r["at"])
        if when.date() != target:
            continue
        sha = SHA_RE.search(str(details.get("note") or details.get("sha") or ""))
        lignes.append({"id_evenement": r["id"], "heure": when.strftime("%H:%M"), "todo_id": r["task_id"],
                       "titre": r["title"], "sorte": sorte, "sha": sha.group(0) if sha else None, "by": by,
                       "annulee": any(i > r["id"] for i, _a in later.get(r["task_id"], []))})
    try:  # todo #534: an unreadable comptes.json never breaks the bilan
        from ultron_hub.compte import auto
        lignes += auto.lignes_bilan(target)
    except Exception:  # noqa: BLE001
        pass
    remises = []
    try:  # todo #534: an unreadable history never breaks the bilan
        from ultron_hub.compte import budget, planning
        remises = planning.bilans_du_jour(budget.read_history(settings.var_dir), target)
    except Exception:  # noqa: BLE001
        pass
    lignes += _hors_todos(settings, target)
    lignes.sort(key=lambda x: x["heure"])
    n_f = sum(1 for x in lignes if x["sorte"] == "fusion")
    n_b = sum(1 for x in lignes if x["sorte"] == "bascule")
    # todo #532: faits = every line of the day; gestes_evites = the ✅ (closure) and 🔀 (merge) the user would have made without the rule,
    # one per line, minus those he undid with ↩️ (an undone line is a gesture he finally had to make himself)
    evites = sum(1 for x in lignes if not x["annulee"])
    return {"date": target.isoformat(), "total": len(lignes), "fusions": n_f,
            "clotures": sum(1 for x in lignes if x["sorte"] == "clôture"), "bascules": n_b,
            "faits": len(lignes), "gestes_evites": evites, "lignes": lignes, "remises": remises}
