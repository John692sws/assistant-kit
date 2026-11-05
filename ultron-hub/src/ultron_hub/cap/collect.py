"""`cap collect` (todo #189): the facts of the last 7 days for the future « revue de cap », gathered WITHOUT AI.

Read only, best effort: every source is read through a read-only door, links are never followed, protected paths
(secrets and exclusions of config/menage.yaml, plus ~/forum and ~/.ssh) are never opened. An unreadable source becomes a
line of `warnings`, never an exception. Total size is kept under BUDGET: chantiers out of the focus are dropped first
and listed in `non_revus`.
"""

import json
import re
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ultron_hub.chantier import core as chantier_core
from ultron_hub.esprit import focus as focus_mod
from ultron_hub.esprit import store as journal
from ultron_hub.menage import audit
from ultron_hub.todo.service import is_delivered

DAYS = 7
BUDGET = 40 * 1024
EXTRACT_MAX = 1500
FALLBACK_MAX = 800
FICHE_READ_MAX = 256 * 1024
JOURNAL_RECENT = 15
TEXT_MAX = 160
COMMITS_MAX = 80
ANOMALIES_MAX = 10
TODOS_MAX = 25
HEADINGS = {"objectif": ("objectif", "but"), "decisions": ("décision", "decision")}
TODO_REF = re.compile(r"(?:todo\s*)?#(\d{1,6})", re.IGNORECASE)


def _size(data: dict[str, Any]) -> int:
    return len(json.dumps(data, ensure_ascii=False).encode("utf-8"))


def _cut(text: Any, limit: int) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[:limit - 1] + "…"


def _ts(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


# --- fiches ---------------------------------------------------------------------------------------------------------

def _extract(text: str, words: tuple[str, ...]) -> str:
    """Body of the first Markdown section whose heading contains one of `words`, capped."""
    for line in text.splitlines():
        if re.match(r"#+\s", line) and any(w in line.lower() for w in words):
            part = audit._section(text, line.lstrip("# ").strip())
            body = "\n".join(part.splitlines()[1:]).strip()
            if body:
                return body[:EXTRACT_MAX]
    return ""


def _fiche(fiche: str, home: str, patterns: list[str], warnings: list[str]) -> dict[str, str]:
    if fiche == "~" or fiche.startswith("~/"):
        fiche = home + fiche[1:]
    path = Path(fiche)
    name = path.name
    notes = Path(home) / "notes"
    if path.parent != notes or audit._is_protected(str(path), patterns, home):
        warnings.append(f"fiche {name} hors de ~/notes ou protégée : non lue")
        return {}
    try:
        text = audit._read_text(path, FICHE_READ_MAX)
    except OSError:
        warnings.append(f"fiche {name} illisible")
        return {}
    out = {k: _extract(text, w) for k, w in HEADINGS.items()}
    if not out["objectif"]:  # no heading: the first paragraph after the title
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip() and not p.lstrip().startswith("#")]
        out["objectif"] = paras[0][:FALLBACK_MAX] if paras else ""
    return out


# --- todos ----------------------------------------------------------------------------------------------------------

def _todos(db: Any, since: float, warnings: list[str]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """{chantier: {ouvertes, livrees, closes, abandonnees}} from a read-only connection."""
    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    if db is None or not Path(db).is_file():
        warnings.append("base des todos introuvable")
        return out
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
        try:
            rows = conn.execute("SELECT id, title, status, chantier, done_at, updated_at FROM tasks "
                                "WHERE chantier IS NOT NULL ORDER BY id").fetchall()
            ev: dict[int, list[tuple[str, dict]]] = {}
            for tid, action, details in conn.execute("SELECT task_id, action, details FROM events ORDER BY id"):
                try:
                    parsed = json.loads(details)
                except (TypeError, ValueError):
                    parsed = {}
                ev.setdefault(tid, []).append((action, parsed if isinstance(parsed, dict) else {}))
        finally:
            conn.close()
    except sqlite3.Error as exc:
        warnings.append(f"todos illisibles : {type(exc).__name__}")
        return out
    for tid, title, status, chantier, done_at, updated_at in rows:
        group = out.setdefault(chantier, {"ouvertes": [], "livrees": [], "closes": [], "abandonnees": []})
        item = {"id": tid, "titre": _cut(title, TEXT_MAX)}
        if status == "todo":
            group["ouvertes"].append(item)
            if is_delivered(ev.get(tid, [])):
                group["livrees"].append(item)
        elif status == "done" and (_ts(done_at) or 0) >= since:
            group["closes"].append(item)
        elif status == "cancelled" and (_ts(updated_at) or 0) >= since:
            group["abandonnees"].append(item)
    for group in out.values():
        for key, items in group.items():
            if len(items) > TODOS_MAX:
                group[key] = items[-TODOS_MAX:]
    return out


# --- other sources --------------------------------------------------------------------------------------------------

def _commits(repo: Path, since: float, warnings: list[str]) -> list[dict[str, Any]]:
    iso = datetime.fromtimestamp(since).astimezone().isoformat(timespec="seconds")
    try:
        done = subprocess.run(["git", "-C", str(repo), "log", "main", f"--since={iso}", "--format=%h%x09%s"],
                              capture_output=True, text=True, timeout=15, env={"GIT_OPTIONAL_LOCKS": "0", "PATH": "/usr/bin:/bin"})
    except (OSError, subprocess.SubprocessError) as exc:
        warnings.append(f"commits illisibles : {type(exc).__name__}")
        return []
    if done.returncode != 0:
        warnings.append("commits de main illisibles")
        return []
    out = []
    for line in done.stdout.splitlines()[:COMMITS_MAX]:
        sha, _, subject = line.partition("\t")
        ref = TODO_REF.findall(subject)
        out.append({"sha": sha, "sujet": _cut(subject, TEXT_MAX), "todo": int(ref[-1]) if ref else None})
    return out


def _journal(settings: Any, since: float, warnings: list[str]) -> dict[str, Any]:
    try:
        rows = [r for r in journal._all_entries(journal.root(settings)) if (_ts(r.get("date")) or 0) >= since]
    except (OSError, TypeError, AttributeError):
        warnings.append("journal commun illisible")
        return {"par_source": {}, "dernieres": []}
    counts: dict[str, int] = {}
    for r in rows:
        key = str(r.get("source") or "?")
        counts[key] = counts.get(key, 0) + 1
    last = [{"date": str(r.get("date") or "")[:16], "source": str(r.get("source") or "?"),
             "texte": _cut(r.get("texte"), TEXT_MAX)} for r in rows[-JOURNAL_RECENT:]]
    return {"par_source": dict(sorted(counts.items(), key=lambda kv: -kv[1])), "dernieres": last}


def _json_file(path: Path, label: str, warnings: list[str]) -> dict[str, Any] | None:
    """Absent = None without warning; unreadable = None with one."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        data = json.loads(audit._read_text(path, FICHE_READ_MAX))
    except (OSError, ValueError):
        warnings.append(f"{label} illisible")
        return None
    return data if isinstance(data, dict) else None


def _reflexes(settings: Any, warnings: list[str]) -> list[dict[str, str]]:
    """Diagnostics of the watcher (var/health/reflexes.json), best effort: absent or odd = empty list."""
    data = _json_file(Path(settings.var_dir) / "health" / "reflexes.json", "var/health/reflexes.json", warnings)
    raw = data.get("diagnostics") if data else None
    out: list[dict[str, str]] = []
    for d in (raw if isinstance(raw, list) else [])[:ANOMALIES_MAX]:
        if isinstance(d, dict):
            out.append({str(k)[:40]: _cut(v, TEXT_MAX) for k, v in list(d.items())[:6]})
        elif isinstance(d, str):
            out.append({"diagnostic": _cut(d, TEXT_MAX)})
    return out


def _failures(settings: Any, warnings: list[str]) -> dict[str, Any]:
    last = _json_file(Path(settings.var_dir) / "menage" / "last.json", "var/menage/last.json", warnings)
    menage = None
    if last is not None:
        anomalies = last.get("anomalies") if isinstance(last.get("anomalies"), list) else []
        menage = {"comptes": last.get("counts") if isinstance(last.get("counts"), dict) else {},
                  "anomalies": [{"check": a.get("check"), "detail": _cut(a.get("detail"), TEXT_MAX)}
                                for a in anomalies[:ANOMALIES_MAX] if isinstance(a, dict)],
                  "total": len(anomalies)}
    return {"menage": menage, "reflexes": _reflexes(settings, warnings)}


# --- chantiers ------------------------------------------------------------------------------------------------------

def _chantiers(settings: Any, src: Any, home: str, patterns: list[str], since: float, warnings: list[str]
               ) -> list[dict[str, Any]]:
    try:
        board = chantier_core.tableau(settings, src)
    except Exception as exc:  # noqa: BLE001 - best effort
        warnings.append(f"tableau des chantiers illisible : {type(exc).__name__}")
        return []
    warnings.extend(board.get("avertissements") or [])
    focus = focus_mod.read(settings)
    todos = _todos(settings.todo_db, since, warnings)
    out = []
    for c in board["chantiers"]:
        name = c["sujet"]
        item = {"nom": name, "titre": c["titre"], "domaine": c["domaine"],
                "focus": focus_mod.dans_le_focus(focus, c["domaine"]), "groupe": c["groupe"],
                "ou_en_est": c["ou_en_est"], "a_toi": [{"kind": a["kind"], "todo": a.get("todo"),
                                                         "texte": _cut(a["texte"], TEXT_MAX)} for a in c["a_toi"]][:TODOS_MAX],
                **_fiche(c["fiche"], home, patterns, warnings),
                "todos": todos.get(name) or {"ouvertes": [], "livrees": [], "closes": [], "abandonnees": []}}
        out.append(item)
    out.sort(key=lambda i: not i["focus"])  # stable: focus first
    return out


def collect(settings: Any, *, home: str | None = None, now: float | None = None, src: Any = None,
            repo: Path | None = None) -> dict[str, Any]:
    """The facts of the last 7 days. Never raises because of the disk; see the module docstring."""
    home = home or audit.default_home()
    now = time.time() if now is None else now
    since = now - DAYS * 86400
    warnings: list[str] = []
    try:
        yaml_text = audit._read_text(Path(settings.root) / "config" / "menage.yaml", audit.YAML_MAX)
    except OSError:
        yaml_text = ""
    patterns = audit._protected_patterns(yaml_text, home, warnings)
    try:
        src = src or chantier_core.sources()
        chantiers = _chantiers(settings, src, home, patterns, since, warnings)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"chantiers illisibles : {type(exc).__name__}")
        chantiers = []
    previous = _json_file(Path(settings.var_dir) / "cap" / "last.json", "var/cap/last.json", warnings)
    focus = focus_mod.read(settings)
    data: dict[str, Any] = {
        "depuis": datetime.fromtimestamp(since).astimezone().isoformat(timespec="seconds"),
        "jusqu_a": datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
        "focus": focus["domaines"] if focus else [],
        "chantiers": chantiers,
        "commits": _commits(Path(repo or settings.root), since, warnings),
        "journal": _journal(settings, since, warnings),
        "echecs": _failures(settings, warnings),
        "revue_precedente": previous,
        "non_revus": [],
        "warnings": warnings,
    }
    _fit(data)
    return data


def _fit(data: dict[str, Any]) -> None:
    """Stay under BUDGET: chantiers out of the focus first (last first), then the journal and the commits."""
    while _size(data) > BUDGET and any(not c["focus"] for c in data["chantiers"]):
        idx = max(i for i, c in enumerate(data["chantiers"]) if not c["focus"])
        data["non_revus"].append(data["chantiers"].pop(idx)["nom"])
    data["non_revus"].reverse()
    if data["non_revus"]:
        data["warnings"].append(f"{len(data['non_revus'])} chantier(s) non revus faute de place")
    while _size(data) > BUDGET and data["journal"]["dernieres"]:
        data["journal"]["dernieres"].pop(0)
    while _size(data) > BUDGET and len(data["commits"]) > 10:
        data["commits"].pop()
    for key in ("decisions", "objectif"):  # last resort: halve the extracts of the focus chantiers
        for c in data["chantiers"]:
            while c.get(key) and _size(data) > BUDGET:
                c[key] = c[key][:len(c[key]) // 2] if len(c[key]) > 80 else ""
