"""`cout compare`: the last N sessions of each mode (squad, interactive) side by side. Read only, no AI, no network."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ultron_hub.cout import session
from ultron_hub.cout import usage_jsonl

DEFAULT_PROJECTS = Path("~/.claude/projects")
# project folders that are not the user's interactive sessions (mission workspaces, squad essays, scratchpads)
SKIP_PROJECT_PARTS = ("-var-missions-", "-notes-escouade-", "scratchpad")
UNITE = ("USD. Interactif : estimation depuis les tokens (table de prix interne). "
         "Escouade : coût relevé par Claude (total_cost_usd), estimation seulement si la session est coupée.")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def squad_session_ids(state: Path) -> set[str]:
    """Claude session ids used by squads (their transcripts are not interactive sessions)."""
    ids: set[str] = set()
    for squad in state.glob("*/"):
        sid = _read_json(squad / "etat.json").get("session_id")
        if sid:
            ids.add(str(sid))
        for stream in squad.glob("session-*.jsonl"):
            try:
                with stream.open(encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        try:
                            found = json.loads(line).get("session_id")
                        except (ValueError, AttributeError):
                            continue
                        if found:
                            ids.add(str(found))
                            break
            except OSError:
                continue
    return ids


def _squad_points(squad: Path, etat: dict[str, Any]) -> float | None:
    if isinstance(etat.get("points_consommes"), (int, float)):
        return float(etat["points_consommes"])
    garde = _read_json(squad / "garde.json")
    if isinstance(garde.get("soft_semaine"), (int, float)) and isinstance(garde.get("base_semaine"), (int, float)):
        return round(garde["soft_semaine"] - garde["base_semaine"], 2)
    delta = _read_json(squad / "quota.json").get("delta_semaine")
    return float(delta) if isinstance(delta, (int, float)) else None


def squad_sessions(state: Path, repo: Path, n: int) -> list[dict[str, Any]]:
    rows = []
    for squad in state.glob("*/"):
        etat = _read_json(squad / "etat.json")
        streams = list(squad.glob("session-*.jsonl"))
        if not etat or not streams:
            continue
        end = max(s.stat().st_mtime for s in streams)
        start = etat.get("debut_ts") if isinstance(etat.get("debut_ts"), (int, float)) else squad.stat().st_ctime
        conso = usage_jsonl.consommation(squad)
        todos = session.delivered_todos(repo, datetime.fromtimestamp(start, timezone.utc),
                                        datetime.fromtimestamp(end, timezone.utc))
        rows.append({"id": squad.name, "debut": _iso(start), "duree_s": max(0, round(end - start)),
                     "cout_usd": round(conso["cout_usd"], 4), "points": _squad_points(squad, etat),
                     "todos_livrees": todos, "_ts": start})
    return sorted(rows, key=lambda r: r["_ts"], reverse=True)[:n]


def interactive_sessions(projects: Path, state: Path, repo: Path, n: int) -> list[dict[str, Any]]:
    skip = squad_session_ids(state)
    files = []
    for folder in projects.glob("-home-ultron*/"):
        if any(part in folder.name for part in SKIP_PROJECT_PARTS):
            continue
        files += [f for f in folder.glob("*.jsonl") if f.stem not in skip]
    rows = []
    for path in sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)[:n]:
        d = session.measure(path, repo)
        if not d["messages"]:
            continue
        rows.append({"id": path.stem[:8], "debut": d["debut"], "duree_s": d["duree_s"], "cout_usd": d["cout_usd"],
                     "points": None, "todos_livrees": d["todos_livrees"]})
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cout = round(sum(r["cout_usd"] for r in rows), 4)
    todos = sum(len(r["todos_livrees"]) for r in rows)
    known = [r for r in rows if r["points"] is not None]
    points = round(sum(r["points"] for r in known), 2) if known else None
    points_todos = sum(len(r["todos_livrees"]) for r in known)
    return {"sessions": len(rows), "cout_usd": cout, "todos_livrees": todos,
            "cout_par_todo": round(cout / todos, 4) if todos else None,
            "points": points, "points_par_todo": round(points / points_todos, 2) if points is not None and points_todos else None}


def _fmt(value: Any, suffix: str = "") -> str:
    return "?" if value is None else f"{value}{suffix}"


def render(modes: dict[str, dict[str, Any]]) -> str:
    out = ["Coût des deux modes", f"Unité : {UNITE}"]
    for name, mode in modes.items():
        out.append(f"\n{name} ({mode['resume']['sessions']} dernières)")
        for r in mode["sessions"]:
            todos = ", ".join(f"#{t}" for t in r["todos_livrees"]) or "aucune"
            out.append(f"- {r['debut'][:16]} {r['id']} : {r['cout_usd']} USD, {_fmt(r['points'], ' pts')}, todos {todos}")
        s = mode["resume"]
        out.append(f"Total {s['cout_usd']} USD, {s['todos_livrees']} todo(s). Par todo livrée : "
                   f"{_fmt(s['cout_par_todo'], ' USD')}, {_fmt(s['points_par_todo'], ' pts')}.")
    return "\n".join(out)


def compare(n: int, state: Path, projects: Path, repo: Path) -> dict[str, Any]:
    modes = {"escouade": squad_sessions(state, repo, n), "interactif": interactive_sessions(projects, state, repo, n)}
    result = {name: {"sessions": rows, "resume": summarize(rows)} for name, rows in modes.items()}
    for mode in result.values():
        for r in mode["sessions"]:
            r.pop("_ts", None)
    return {"n": n, "unite": UNITE, **result, "text": render(result)}
