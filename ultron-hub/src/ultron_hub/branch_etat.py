"""State of the merges (todo #255): `ultron-hub branch etat`, read only, JSON only on stdout.

Three views for the dashboard, from git, the log of automatic merges (auto_merge) and the todos:
- `en_cours`: every todo-<id>/… branch the orchestrator could merge alone (todo, branch, last commit, worktree clean or not);
- `attente`: the branches waiting for the user's 🔀, and why (the refusals of `auto_merge.refusals`, not a copy of its rules);
- `auto`: the last automatic merges (date, hash, todo still open for the user's ✅), newest first, with `annulation`,
  the target the ↩️ button signs (the same one as `branch auto-last`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ultron_hub import auto_merge, autorisation, branches, repos
from ultron_hub.branches import MAIN, git
from ultron_hub.common.settings import Settings
from ultron_hub.todo import service
from ultron_hub.todo.store import TodoStore

MAX_AUTO = 10
SHA_LEN = auto_merge.SHA12_LEN
SEP = "\x1f"


def _commit(root: Path, branch: str) -> dict | None:
    out = git(root, "log", "-1", f"--format=%H{SEP}%cI{SEP}%s", f"refs/heads/{branch}", check=False)
    sha, _, rest = out.partition(SEP)
    date, _, subject = rest.partition(SEP)
    return {"sha": sha[:SHA_LEN], "date": date, "subject": subject} if sha else None


def _worktree(root: Path, branch: str) -> dict | None:
    path = branches.branch_worktree(root, branch)
    if path is None:
        return None
    dirty = git(path, "status", "--porcelain", check=False)
    return {"path": str(path), "propre": not dirty.strip()}


def _by_todo(settings: Settings) -> dict[int, list[dict]]:
    """Every todo-<id>/… branch of every repository, one target per branch (so `refusals` reads each at its own head)."""
    found: dict[int, list[dict]] = {}
    for name, path in repos.list_repos(settings).items():
        current = git(path, "branch", "--show-current", check=False)
        for ref in git(path, "for-each-ref", "--format=%(refname:short)", "refs/heads/", check=False).splitlines():
            m = branches.BRANCH_RE.fullmatch(ref)
            if m:
                ahead = git(path, "rev-list", "--count", ref, f"^{MAIN}", check=False)
                found.setdefault(int(m[1]), []).append({"repo": name, "path": path, "branch": ref, "branches": [ref],
                                                        "current": current, "ahead": int(ahead) if ahead.isdigit() else 0})
    return found


def _title(todos: TodoStore, todo_id: int) -> str | None:
    try:
        return todos.get(todo_id).title
    except Exception:
        return None


def _row(settings: Settings, todos: TodoStore, todo_id: int, targets: list[dict]) -> dict:
    return {"todo_id": todo_id, "title": _title(todos, todo_id),
            "branches": [{"repo": t["repo"], "branch": t["branch"], "label": branches.label(settings, t["repo"], t["branch"]),
                          "ahead": t["ahead"], "commit": _commit(t["path"], t["branch"]),
                          "worktree": _worktree(t["path"], t["branch"])} for t in targets]}


def _fusions(settings: Settings, todos: TodoStore) -> list[dict]:
    entries = auto_merge._entries(settings)
    reverted = {e.get("cible") for e in entries if e.get("type") == "revert"}
    out = []
    for e in reversed([e for e in entries if e.get("type") == "fusion" and e.get("repos")]):
        todo_id = int(e["todo_id"])
        try:
            task = todos.get(todo_id)
            open_for_user = task.status == "todo" and service.is_delivered(
                [(h["action"], h["details"]) for h in todos.history(todo_id)])
            title = task.title
        except Exception:
            open_for_user, title = False, None
        cible = auto_merge.target(e)
        out.append({"todo_id": todo_id, "title": title, "at": e.get("at"), "cible": cible,
                    "sha": e["repos"][0]["sha"][:SHA_LEN], "annulee": cible in reverted, "a_valider": open_for_user})
        if len(out) >= MAX_AUTO:
            break
    return out


def etat(settings: Settings) -> dict[str, Any]:
    en_cours, attente = [], []
    with TodoStore(settings.todo_db) as todos:
        for todo_id, targets in sorted(_by_todo(settings).items()):
            row = _row(settings, todos, todo_id, targets)
            why = auto_merge.refusals(settings, todo_id, targets)
            if why:
                attente.append({**row, "raisons": [{"step": r["step"], "reason": r["reason"], "files": r["files"]} for r in why]})
            else:
                en_cours.append(row)
        auto = _fusions(settings, todos)
    last = auto_merge.last(settings)
    return {"en_cours": en_cours, "attente": attente, "auto": auto,
            "annulation": {"cible": last["cible"], "annulee": last["annulee"]},
            "autorisation": autorisation.etat(settings)}
