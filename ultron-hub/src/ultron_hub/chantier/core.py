"""State of the "chantiers" (subjects of the ultron sessions), best effort (todo #108); only archiver/rouvrir write (todo #122).

Every source that cannot be read gives zero plus a warning, never an error. Nothing here writes anything.
"""

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ultron_hub import after_merge
from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import Settings
from ultron_hub.escouade import core as escouade_core
from ultron_hub.todo import links, service as todo_service

log = get_logger("chantier")

DEFAULT_BOITES = Path("~/.local/state/ultron-hub/boites")
HORS = "-"  # reserved name of "Hors chantier" (a real subject starts with a letter or a digit)
ARCHIVE_DIR = "termines"
SESSION_MAX = 300
REPRISE_LINES = 5
KINDS = ("question", "fusionner", "livree", "apres_fusion")
ICONS = {"question": "❓", "fusionner": "🔀", "livree": "✅", "apres_fusion": "🧰"}
_TITLE = re.compile(r"«\s*(.+?)\s*»")
_SESSION = re.compile(r"^Session précédente\s*:\s*(.+?)\s*$", re.MULTILINE)
_FICHE = re.compile(r"^Fiche du chantier\s*:\s*`([^`]+)`", re.MULTILINE)
_DOMAINE = re.compile(r"^Domaine\s*:\s*`?([A-Za-z0-9_-]+)`?", re.MULTILINE)
# (key, icon, title): the order of /chantiers; a missing or unknown key goes to A_RANGER
DOMAINES = (("systeme", "🛠", "Système"), ("business", "💼", "Business"), ("perso", "🕌", "Perso"))
A_RANGER = ("", "❔", "À ranger")
_DATE = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")
_BULLET = re.compile(r"^[-*] ")


@dataclass
class Sources:
    """Where the data lives; every folder can be replaced (tests)."""
    prompts: Path
    boites: Path
    warnings: list[str] = field(default_factory=list)

    def warn(self, text: str) -> None:
        log.warning("chantier: %s", text)
        self.warnings.append(text)


def sources(prompts: str | None = None, boites: str | None = None) -> Sources:
    return Sources(prompts=escouade_core.prompts_dir(prompts),
                   boites=Path(boites or DEFAULT_BOITES).expanduser())


def subjects(src: Sources, archived: bool = False) -> list[str]:
    folder = src.prompts / ARCHIVE_DIR if archived else src.prompts
    try:
        return escouade_core.list_subjects(folder)
    except OSError as exc:
        src.warn(f"sujets illisibles : {exc}")
        return []


def parse_prompt(text: str, sujet: str, mtime: float | None = None) -> dict[str, Any]:
    """Title, project, "where we are", date and fiche path from a subject file; a missing header gives defaults."""
    head, _ = escouade_core.split_sections(text)
    first = next((line for line in head.splitlines() if line.strip()), "")
    title = _TITLE.search(first) if first.startswith("#") else None
    try:
        projet = escouade_core.projet(text)
    except Exception:
        projet = None
    session = _SESSION.search(head)
    ou = " ".join(session.group(1).split())[:SESSION_MAX] if session else ""
    date = ""
    if found := (_DATE.search(ou) if ou else None):
        date = f"{found[3]}-{found[2]}-{found[1]}"
    elif mtime:
        date = time.strftime("%Y-%m-%d", time.localtime(mtime))
    fiche = _FICHE.search(head)
    cle = (_DOMAINE.search(head).group(1).lower() if _DOMAINE.search(head) else "")
    dom = next((d for d in DOMAINES if d[0] == cle), A_RANGER)
    return {"domaine": dom[0], "domaine_icone": dom[1], "domaine_titre": dom[2],
            "titre": title.group(1) if title else sujet, "projet": str(projet) if projet else "",
            "ou_en_est": ou, "date": date,
            "fiche": fiche.group(1) if fiche else f"~/notes/chantier-{sujet}.md"}


def reprise(path: str) -> list[str]:
    """Last first-level bullet of the "## REPRISE" section of a fiche (5 lines at most); [] when absent."""
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return []
    marks = list(re.finditer(r"^## REPRISE\b.*$", text, re.MULTILINE))
    if not marks:
        return []
    body = text[marks[-1].end():]
    nxt = re.search(r"^## ", body, re.MULTILINE)
    lines = (body[:nxt.start()] if nxt else body).splitlines()
    starts = [i for i, line in enumerate(lines) if _BULLET.match(line)]
    if not starts:
        return []
    chunk = [lines[starts[-1]]]
    for line in lines[starts[-1] + 1:]:
        if _BULLET.match(line) or not line.strip():
            break
        chunk.append(line)
    return [line.rstrip() for line in chunk[:REPRISE_LINES]]


def open_todos(settings: Settings) -> tuple[list[dict[str, Any]], bool, list[str]]:
    """Open todos read straight from the database (read-only connection, no migration).

    Returns (rows {id, title, chantier}, whether the `chantier` column exists, warnings).
    """
    db = settings.todo_db
    if db is None or not Path(db).is_file():
        return [], False, ["base des todos introuvable"]
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            has = any(r["name"] == "chantier" for r in conn.execute("PRAGMA table_info(tasks)"))
            cols = "id, title, " + ("chantier" if has else "NULL AS chantier")
            rows = conn.execute(f"SELECT {cols} FROM tasks WHERE status = 'todo' ORDER BY id").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return [], False, [f"todos illisibles : {type(exc).__name__}"]
    return [dict(r) for r in rows], has, []


def done_counts(settings: Settings) -> dict[str, int]:
    """Done todos per chantier, read-only (the x/y of the dashboard's home screen, todo #281); {} without the column."""
    db = settings.todo_db
    if db is None or not Path(db).is_file():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
        try:
            if not any(r[1] == "chantier" for r in conn.execute("PRAGMA table_info(tasks)")):
                return {}
            rows = conn.execute("SELECT chantier, COUNT(*) FROM tasks WHERE status = 'done' AND chantier IS NOT NULL "
                                "GROUP BY chantier").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return {str(name): int(n) for name, n in rows}


def todos_of(settings: Settings, sujet: str, rows: tuple[list[dict[str, Any]], bool, list[str]] | None = None
             ) -> tuple[list[dict[str, Any]], list[str]]:
    """Open todos of a chantier. Isolated on purpose: todo #103 adds the `chantier` column (rebase on it).

    Without the column: ([], [warning]).
    """
    todos, has, warnings = rows if rows is not None else open_todos(settings)
    if not has:
        return [], warnings + ["colonne chantier absente (todo #103) : aucune todo rattachée"]
    return [t for t in todos if t["chantier"] == sujet], warnings


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def questions_open(src: Sources, sujet: str) -> list[dict[str, Any]]:
    """Open questions of the chantier's mailbox (todo #106 writes it); [] when the folder is absent."""
    folder = src.boites / sujet / "questions"
    if not folder.is_dir():
        return []
    out = []
    try:
        files = sorted(folder.glob("*.json"))
    except OSError as exc:
        src.warn(f"{sujet} : questions illisibles ({exc})")
        return []
    for path in files:
        data = _read_json(path)
        if data is None:
            src.warn(f"{sujet} : question illisible ({path.name})")
        elif data.get("statut") == "ouverte" and not (folder.parent / "reponses" / f"{path.stem}.json").is_file():
            out.append({"id": str(data.get("id") or path.stem), "texte": str(data.get("texte") or "")[:200],
                        "bloquante": bool(data.get("bloquante")), "echeance": data.get("echeance")})
    return out


REMARQUES_SHOWN = 3  # the card shows the last three remarks


def remarques(src: Sources, sujet: str) -> dict[str, Any]:
    """{en_attente, lues, derniere_lue, dernieres}: remarks dropped and not yet read, read ones, last read time,
    and the last three remarks (newest first) as {texte, at, lue_at|None}; no seal, no proof (todo #119)."""
    base = src.boites / sujet
    waiting = list((base / "remarques").glob("*.json")) if (base / "remarques").is_dir() else []
    read = list((base / "lues").glob("*.json")) if (base / "lues").is_dir() else []
    last = ""
    recent: list[dict[str, Any]] = []
    for path in [*read, *waiting]:
        data = _read_json(path)
        if not data:
            continue
        lue = data.get("lue_at") if path.parent.name == "lues" else None
        if lue is not None and str(lue) > last:
            last = str(lue)
        try:
            at = float(data.get("at") or 0)
            lue = float(lue) if lue is not None else None
        except (TypeError, ValueError):
            at, lue = 0.0, None
        recent.append({"texte": " ".join(str(data.get("texte") or "").split())[:80], "at": at, "lue_at": lue})
    recent.sort(key=lambda r: r["at"], reverse=True)
    return {"en_attente": len(waiting), "lues": len(read), "derniere_lue": last,
            "dernieres": recent[:REMARQUES_SHOWN]}


@dataclass
class World:
    """Everything shared by all chantiers, computed once per command."""
    src: Sources
    settings: Settings
    todos: tuple[list[dict[str, Any]], bool, list[str]]
    branches: dict[int, dict[str, Any]]
    livrees: frozenset[int] = frozenset()  # open todos declared delivered (todo #153)
    fermees: tuple[dict[str, Any], ...] = ()  # CLOSED todos with an after-merge action still to run (todo #187)


def delivered_ids(settings: Settings, ids: list[int]) -> frozenset[int]:
    """Open todos whose last event is `livree` (rule: `todo.service.is_delivered`). Unreadable events: none."""
    db = settings.todo_db
    if not ids or db is None or not Path(db).is_file():
        return frozenset()
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
        try:
            marks = ",".join("?" * len(ids))
            rows = conn.execute(f"SELECT task_id, action, details FROM events WHERE task_id IN ({marks}) ORDER BY id",
                                ids).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return frozenset()
    events: dict[int, list[tuple[str, dict]]] = {}
    for tid, action, details in rows:
        try:
            parsed = json.loads(details)
        except (TypeError, ValueError):
            parsed = {}
        events.setdefault(tid, []).append((action, parsed if isinstance(parsed, dict) else {}))
    return frozenset(t for t, ev in events.items() if todo_service.is_delivered(ev))


def closed_with_actions(settings: Settings, open_ids: set[int]) -> tuple[dict[str, Any], ...]:
    """Closed todos whose var/after-merge/todo-<id>.json keeps an action `pending` or `interrupted` (todo #187).

    The todo is closed by the merge itself, so only this file still says that something waits. Rows {id, title,
    chantier}; unreadable files or database: none.
    """
    var = getattr(settings, "var_dir", None)
    if var is None:
        return ()
    folder = Path(var) / "after-merge"
    ids = []
    try:
        names = sorted(folder.glob("todo-*.json"))
    except OSError:
        return ()
    for path in names:
        m = re.fullmatch(r"todo-([0-9]{1,9})\.json", path.name)
        if m and int(m.group(1)) not in open_ids and _after_merge_pending_of(settings, int(m.group(1))):
            ids.append(int(m.group(1)))
    db = settings.todo_db
    if not ids or db is None or not Path(db).is_file():
        return ()
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            has = any(r["name"] == "chantier" for r in conn.execute("PRAGMA table_info(tasks)"))
            cols = "id, title, " + ("chantier" if has else "NULL AS chantier")
            marks = ",".join("?" * len(ids))
            rows = conn.execute(f"SELECT {cols} FROM tasks WHERE status != 'todo' AND id IN ({marks}) ORDER BY id",
                                ids).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return ()
    return tuple(dict(r) for r in rows)


def world(settings: Settings, src: Sources) -> World:
    todos = open_todos(settings)
    src.warnings.extend(todos[2])
    if not todos[1]:
        src.warn("colonne chantier absente (todo #103) : toutes les todos sont hors chantier")
    return World(src=src, settings=settings, todos=todos,
                 branches=links.branch_map(settings),
                 livrees=delivered_ids(settings, [int(t["id"]) for t in todos[0]]),
                 fermees=closed_with_actions(settings, {int(t["id"]) for t in todos[0]}))


def _after_merge_pending_of(settings: Settings, todo_id: int) -> int:
    try:
        return len(after_merge.pending(after_merge.load(settings, todo_id)["actions"]))
    except Exception:
        return 0


def _after_merge_pending(w: World, todo_id: int) -> int:
    return _after_merge_pending_of(w.settings, todo_id)


def todo_item(w: World, todo: dict[str, Any]) -> dict[str, Any] | None:
    """What this open todo asks of the user, first match: 🔀, ✅ (delivered), 🧰; None when nothing."""
    tid = int(todo["id"])
    branch = w.branches.get(tid)
    kind = None
    if branch and branch.get("ahead"):
        kind = "fusionner"
    elif tid in w.livrees:
        kind = "livree"
    elif _after_merge_pending(w, tid):
        kind = "apres_fusion"
    if kind is None:
        return None
    return {"kind": kind, "icone": ICONS[kind], "todo": tid, "texte": todo["title"]}


def chantier_state(w: World, sujet: str, mtime: float | None = None) -> dict[str, Any]:
    """Full state of one chantier (or of Hors chantier when sujet == HORS)."""
    src = w.src
    out: dict[str, Any] = {"sujet": sujet, "hors": sujet == HORS}
    if sujet == HORS:
        out.update(titre="Hors chantier", projet="", ou_en_est="", date="", fiche="", domaine=A_RANGER[0],
                   domaine_icone=A_RANGER[1], domaine_titre=A_RANGER[2])
        has = w.todos[1]
        todos = [t for t in w.todos[0] if not has or not t["chantier"]]
        out["reprise"] = []
        questions: list[dict[str, Any]] = []
        out["remarques"] = {"en_attente": 0, "lues": 0, "derniere_lue": "", "dernieres": []}
    else:
        path = src.prompts / f"{sujet}.md"
        try:
            text = path.read_text(encoding="utf-8")
            out.update(parse_prompt(text, sujet, path.stat().st_mtime))
        except OSError as exc:
            src.warn(f"{sujet} : prompt illisible ({type(exc).__name__})")
            out.update(parse_prompt("", sujet, mtime))
        out["reprise"] = reprise(out["fiche"])
        todos, _ = todos_of(w.settings, sujet, w.todos)
        questions = questions_open(src, sujet)
        out["remarques"] = remarques(src, sujet)
    items = [it for it in (todo_item(w, t) for t in todos) if it]
    for t in w.fermees:  # closed by the merge, actions still waiting (todo #187)
        mine = (t["chantier"] is None or not w.todos[1]) if sujet == HORS else t["chantier"] == sujet
        if mine:
            items.append({"kind": "apres_fusion", "icone": ICONS["apres_fusion"], "todo": int(t["id"]), "texte": t["title"]})
    for q in questions:
        items.append({"kind": "question", "icone": ICONS["question"], "question": q["id"], "texte": q["texte"],
                      "bloquante": q["bloquante"]})
    order = {k: i for i, k in enumerate(KINDS)}
    items.sort(key=lambda it: (order[it["kind"]], it.get("todo", 0)))
    for it in items:
        it["chantier"] = sujet
    out["todos"] = [{"id": t["id"], "titre": t["title"]} for t in todos]
    out["questions"] = questions
    out["a_toi"] = items
    out["groupe"] = "📥" if items else "💤"
    return out


def tableau(settings: Settings, src: Sources) -> dict[str, Any]:
    w = world(settings, src)
    chantiers = []
    for name in subjects(src):
        try:
            mtime = (src.prompts / f"{name}.md").stat().st_mtime
        except OSError:
            mtime = None
        chantiers.append(chantier_state(w, name, mtime))
    hors = chantier_state(w, HORS)
    rank = {"🟢": 0, "📥": 1, "💤": 2}
    chantiers.sort(key=lambda c: c["date"], reverse=True)  # stable: most recent first within a group
    chantiers.sort(key=lambda c: (rank[c["groupe"]], -len(c["a_toi"]) if c["groupe"] == "📥" else 0))
    return {"chantiers": chantiers, "hors_chantier": hors, "archives": subjects(src, archived=True),
            "avertissements": src.warnings}


def fiche(settings: Settings, src: Sources, sujet: str) -> dict[str, Any]:
    if sujet != HORS:
        escouade_core.check_name(sujet)
    w = world(settings, src)
    state = chantier_state(w, sujet)
    state["avertissements"] = src.warnings
    return state


def atoi(settings: Settings, src: Sources, chantier: str | None = None) -> dict[str, Any]:
    """Everything that waits for the user, one line each with its chantier; todos without chantier last."""
    if chantier and chantier != HORS:
        escouade_core.check_name(chantier)
    w = world(settings, src)
    names = [chantier] if chantier else subjects(src) + [HORS]
    items: list[dict[str, Any]] = []
    for name in names:
        items.extend(chantier_state(w, name)["a_toi"])
    order = {k: i for i, k in enumerate(KINDS)}
    items.sort(key=lambda it: (it["chantier"] == HORS, order[it["kind"]],
                               not it.get("bloquante", False), it["chantier"], it.get("todo", 0)))
    return {"items": items, "total": len(items), "avertissements": src.warnings}


def _check_archive_name(sujet: str) -> str:
    if not escouade_core.NAME_RE.fullmatch(sujet or ""):
        raise InvalidRequestError(f"nom de chantier refusé : {sujet!r} (a-z, 0-9 et tirets, 40 caractères au plus)")
    if sujet == escouade_core.MAIN_NAME:
        raise InvalidRequestError("« main » est le nom de la session Main (sans chantier) : il ne peut pas être un chantier")
    return sujet


def _move(source: Path, target: Path) -> None:
    """Move a regular file without ever overwriting: hard link then unlink (os.rename would replace silently)."""
    target.parent.mkdir(exist_ok=True)
    if target.parent.is_symlink() or not target.parent.is_dir():
        raise InvalidRequestError(f"{target.parent} n'est pas un vrai dossier")
    if os.path.lexists(target):
        raise InvalidRequestError(f"{target.name} existe déjà dans {target.parent} : rien n'a été déplacé")
    try:
        os.link(source, target)
    except FileExistsError:
        raise InvalidRequestError(f"{target.name} existe déjà dans {target.parent} : rien n'a été déplacé") from None
    source.unlink()


def _regular(path: Path, what: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise InvalidRequestError(f"{what} : {path} n'est pas un fichier ordinaire")


def archiver(settings: Settings, src: Sources, sujet: str) -> dict[str, Any]:
    """Move `<prompts>/<s>.md` to `<prompts>/termines/`. Refused while an open todo remains."""
    _check_archive_name(sujet)
    source = src.prompts / f"{sujet}.md"
    if not os.path.lexists(source):
        raise InvalidRequestError(f"chantier inconnu : {sujet!r} (aucun {sujet}.md dans {src.prompts})")
    _regular(source, "archivage refusé")
    rows, has, warnings = open_todos(settings)
    if warnings and not rows and not has:
        raise InvalidRequestError("archivage refusé : les todos sont illisibles, impossible de vérifier "
                                  f"({warnings[0]})")
    todos, _ = todos_of(settings, sujet, (rows, has, warnings))
    if todos:
        ids = ", ".join(f"#{t['id']}" for t in todos[:10])
        raise InvalidRequestError(f"archivage refusé : {len(todos)} todo(s) ouverte(s) dans {sujet} ({ids})")
    target = src.prompts / ARCHIVE_DIR / f"{sujet}.md"
    _move(source, target)
    return {"sujet": sujet, "de": str(source), "vers": str(target)}


def rouvrir(src: Sources, sujet: str) -> dict[str, Any]:
    """Put `<prompts>/termines/<s>.md` back in the prompts folder."""
    _check_archive_name(sujet)
    source = src.prompts / ARCHIVE_DIR / f"{sujet}.md"
    if not os.path.lexists(source):
        raise InvalidRequestError(f"chantier archivé inconnu : {sujet!r} (aucun {sujet}.md dans "
                                  f"{src.prompts / ARCHIVE_DIR})")
    _regular(source, "réouverture refusée")
    if (src.prompts / ARCHIVE_DIR).is_symlink():
        raise InvalidRequestError("réouverture refusée : termines/ est un lien")
    target = src.prompts / f"{sujet}.md"
    _move(source, target)
    return {"sujet": sujet, "de": str(source), "vers": str(target)}
