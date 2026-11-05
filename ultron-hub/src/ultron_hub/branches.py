"""Code branches linked to todos (`todo-<id>/<name>`), merged into main when the todo is closed.

A todo's branches may live in several repositories (`repos.list_repos`: ultron-hub and its neighbours). The merge is only done if, in every one of them, a trial
merge in a throwaway worktree has no conflict and the repository's tests pass on the merged result (in the
bwrap sandbox, see `repos.run_tests`). The live project tree (the code Hermes runs) must be on main or on
the branch being merged: merging from any other branch would silently drop that branch's code.
"""

import fcntl
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, NoReturn

from ultron_hub import after_merge, repos
from ultron_hub.merge_progress import Progress, merged_ids
from ultron_hub.common.errors import InvalidRequestError, UltronHubError
from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import Settings
from ultron_hub.esprit import store as journal
from ultron_hub.todo.service import resolve_hermes
from ultron_hub.todo.store import TodoStore

log = get_logger("branches")

MAIN = "main"
BRANCH_RE = re.compile(r"todo-(\d+)/[A-Za-z0-9._-]{1,60}")
GIT_TIMEOUT_S = 60
SYSTEMCTL = "/usr/bin/systemctl"
RESTART_DELAY_S = 5  # lets the plugin answer on Telegram before the gateway restarts
MAX_COMMITS_SHOWN = 10
MAX_FILES_SHOWN = 10

Restarter = Callable[[], None]


def _env() -> dict[str, str]:
    return {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "GIT_TERMINAL_PROMPT": "0",
            "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"}


def git(root: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          timeout=GIT_TIMEOUT_S, env=_env(), check=False)
    if check and proc.returncode != 0:
        raise UltronHubError(f"git {args[0]} : {(proc.stderr or proc.stdout).strip()[-400:]}")
    return proc.stdout.strip()


def todo_branches(root: Path, todo_id: int) -> list[str]:
    refs = git(root, "for-each-ref", "--format=%(refname:short)", f"refs/heads/todo-{todo_id}/")
    return [r for r in refs.splitlines() if (m := BRANCH_RE.fullmatch(r)) and int(m[1]) == todo_id]


def label(settings: Settings, repo: str, branch: str) -> str:
    """How a branch is shown: the repository is named unless it is ultron-hub itself."""
    return branch if repo == settings.root.name else f"{branch} ({repo})"


def _todo_refs(path: Path) -> list[str]:
    return [r for r in git(path, "for-each-ref", "--format=%(refname:short)", "refs/heads/", check=False).splitlines()
            if BRANCH_RE.fullmatch(r)]


def own_commits(path: Path, todo_id: int, names: list[str], every: list[str] | None = None) -> list[str]:
    """Subjects of the commits of the todo's branches that are in neither main nor another todo's branch.

    A branch built on top of ours (it contains our tip) does not take our commits away: the parent stays ready.
    A branch stacked on another todo has only the commits made since the fork as its own (todo #286)."""
    every = _todo_refs(path) if every is None else every
    tips = [git(path, "rev-parse", "--verify", f"refs/heads/{n}", check=False) for n in names]
    others = []
    for ref in every:
        if ref in names or int(BRANCH_RE.fullmatch(ref)[1]) == todo_id:
            continue
        stacked_on_us = all(tip and subprocess.run(
            ["git", "-C", str(path), "merge-base", "--is-ancestor", tip, ref], capture_output=True, env=_env(),
            timeout=GIT_TIMEOUT_S, check=False).returncode == 0 for tip in tips)
        if not stacked_on_us:
            others.append(f"^{ref}")
    return git(path, "log", "--format=%s", *names, f"^{MAIN}", *others, check=False).splitlines()


def dirty_worktrees(path: Path, names: list[str]) -> list[str]:
    """Worktrees where one of the branches is checked out and `git status` is not empty (untracked files count)."""
    out = []
    for n in names:
        wt = branch_worktree(path, n)
        if wt and git(wt, "status", "--porcelain", check=False):
            out.append(str(wt))
    return out


def readiness(settings: Settings, todo_id: int, found: list[dict] | None = None) -> dict:
    """Is the todo ready to merge (todo #286)? Only then does any 🔀 exist (dashboard, Telegram, Tout fusionner).

    Ready = at least one clean commit (not in main, not in another todo's branch) whose subject ends with
    « (todo #<id>) », and every worktree holding one of its branches is clean. Otherwise the todo is « en cours ».
    Returns {"pret": bool, "raison": str | None, "propres": int}."""
    found = targets(settings, todo_id) if found is None else found
    return _readiness(todo_id, [(t["path"], t["branches"], t["own"]) for t in found])


def _readiness(todo_id: int, per_repo: list[tuple[Path, list[str], list[str]]]) -> dict:
    """`per_repo`: (repository path, the todo's branches there, subjects of their own commits)."""
    tag = f"(todo #{todo_id})"
    if not per_repo:
        return {"pret": False, "raison": "aucune branche", "propres": 0}
    propres = sum(1 for _, _, own in per_repo for subject in own if subject.endswith(tag))
    if not propres:
        return {"pret": False, "propres": 0,
                "raison": f"aucun commit propre dont le message finit par « {tag} » (tâche en cours)"}
    dirty = [wt for path, names, _ in per_repo for wt in dirty_worktrees(path, names)]
    if dirty:
        return {"pret": False, "propres": propres,
                "raison": f"le worktree {dirty[0]} a des modifications non commitées ou des fichiers non suivis"}
    return {"pret": True, "raison": None, "propres": propres}


def tagged_own_commits(bs: list[dict]) -> int:
    """Clean commits « (todo #<id>) » of one todo's branches (`bs`: dicts with todo_id, path, branch), all repos."""
    count = 0
    for path in {b["path"] for b in bs}:
        mine = [b for b in bs if b["path"] == path]
        todo_id = mine[0]["todo_id"]
        count += sum(1 for s in own_commits(path, todo_id, [b["branch"] for b in mine]) if s.endswith(f"(todo #{todo_id})"))
    return count


def targets(settings: Settings, todo_id: int) -> list[dict]:
    """The repositories having todo-<id>/… branches, with their commits not yet in main."""
    found = []
    for name, path in repos.list_repos(settings).items():
        names = todo_branches(path, todo_id)
        if not names:
            continue
        branch = names[0] if len(names) == 1 else None
        # All of the todo's branches count, so that several branches in one repository still show their work.
        commits = git(path, "log", "--format=%s", *names, f"^{MAIN}").splitlines()
        found.append({"repo": name, "path": path, "branch": branch, "branches": names, "ahead": len(commits),
                      "commits": commits, "current": git(path, "branch", "--show-current"),
                      "own": own_commits(path, todo_id, names)})
    return found


def info(settings: Settings, todo_id: int) -> dict:
    """Branches linked to a todo, in every repository (fast: git only, no tests)."""
    found = targets(settings, todo_id)
    unique = bool(found) and all(t["branch"] for t in found)
    commits = [c for t in found for c in t["commits"]]
    ready = readiness(settings, todo_id, found)
    return {"todo_id": todo_id, "pret": ready["pret"], "raison": ready["raison"], "branch": ", ".join(label(settings, t["repo"], t["branch"]) for t in found) if unique
            else None, "branches": [label(settings, t["repo"], b) for t in found for b in t["branches"]],
            "ahead": len(commits), "commits": commits[:MAX_COMMITS_SHOWN], "unique": unique,
            "repos": [{k: v for k, v in t.items() if k not in ("path", "commits", "own")} for t in found]}


def branch_map(settings: Settings) -> dict[int, dict]:
    """{todo_id: {"branch", "ahead", "hors_main", "pret", "raison"}} for every todo-<id>/… branch of every repository.

    `hors_main` counts the commits not in main (commits counted once per repository, same rule as `targets`).
    `pret` is `readiness` (todo #286); `ahead` is `hors_main` for a ready todo and 0 otherwise, so that every
    consumer testing `ahead` (🔀, 🌿 « à fusionner », boards) shows nothing for a todo that is still in progress."""
    found: dict[int, dict] = {}
    pending: dict[int, list] = {}
    for name, path in repos.list_repos(settings).items():
        every = _todo_refs(path)
        refs: dict[int, list[str]] = {}
        for ref in every:
            refs.setdefault(int(BRANCH_RE.fullmatch(ref)[1]), []).append(ref)
        for todo_id, names in refs.items():
            ahead = git(path, "rev-list", "--count", *names, f"^{MAIN}", check=False)
            entry = found.setdefault(todo_id, {"branch": "", "hors_main": 0})
            labels = ", ".join(label(settings, name, ref) for ref in names)
            entry["branch"] = ", ".join(filter(None, (entry["branch"], labels)))
            entry["hors_main"] += int(ahead) if ahead.isdigit() else 0
            pending.setdefault(todo_id, []).append((path, names, own_commits(path, todo_id, names, every)))
    for todo_id, entry in found.items():
        ready = _readiness(todo_id, pending[todo_id])
        pret = ready["pret"] and entry["hors_main"] > 0
        entry.update(pret=pret, raison=ready["raison"], ahead=entry["hors_main"] if pret else 0)
    return found


def branch_worktree(root: Path, branch: str) -> Path | None:
    """Linked worktree (not the project tree itself) where `branch` is checked out, if any."""
    current: Path | None = None
    for line in git(root, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            current = Path(line.removeprefix("worktree "))
        elif line == f"branch refs/heads/{branch}" and current and current.resolve() != root.resolve():
            return current
    return None


def sweep_merge_check(settings: Settings) -> list[str]:
    """Remove the throwaway worktrees a killed merge left in var/merge-check (todo #401: wt-urfrhowy, made on 20/10
    at 23:17 with a half-done trial merge, was still registered on 25/10: `finally` does not run on SIGKILL).
    Called under the merge lock only: then no trial merge or trial revert is running and every entry is stale."""
    base = settings.var_dir / "merge-check"
    if not base.is_dir():
        return []
    removed = []
    for work in sorted(base.glob("wt-*")) + sorted(base.glob("rv-*")):
        if not work.is_dir():
            continue
        if (work / ".git").is_file():  # run from inside: git finds its own repository, whichever it is
            git(work, "worktree", "remove", "--force", str(work), check=False)
        shutil.rmtree(work, ignore_errors=True)
        removed.append(work.name)
    if removed:
        log.warning("step=merge-check-sweep removed stale throwaway worktree(s): %s", ", ".join(removed))
        for path in repos.list_repos(settings).values():
            git(path, "worktree", "prune", check=False)
    return removed


def trial_merge(settings: Settings, name: str, root: Path, branch: str,
                on_tests: Callable[[], None] | None = None) -> dict:
    """Merge `branch` into main in a throwaway worktree and run the repository's tests there (sandboxed).
    Runs under the merge lock (`merge_and_close`)."""
    base = settings.var_dir / "merge-check"
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="wt-", dir=base))
    try:
        git(root, "worktree", "add", "--detach", "--force", str(work), MAIN)
        merge = subprocess.run(["git", "-C", str(work), "merge", "--no-ff", "--no-commit", branch],
                               capture_output=True, text=True, timeout=GIT_TIMEOUT_S, env=_env(), check=False)
        if merge.returncode != 0:
            conflicts = git(work, "diff", "--name-only", "--diff-filter=U", check=False).splitlines()
            detail = ("conflits : " + ", ".join(conflicts)) if conflicts else (merge.stdout + merge.stderr).strip()[-300:]
            return {"ok": False, "stage": "conflict", "detail": detail, "files": conflicts}
        # ultron-hub: pytest's `pythonpath = ["src", "."]` puts the merged code before the editable install.
        if on_tests:
            on_tests()
        tests = repos.run_tests(settings, name, root, work)
        out = {"ok": tests["ok"], "stage": None if tests["ok"] else "tests", "detail": tests["detail"],
               "tested": tests["tested"]}
        if tests.get("timeout"):
            out["timeout"] = True
        return out
    finally:
        git(root, "worktree", "remove", "--force", str(work), check=False)
        shutil.rmtree(work, ignore_errors=True)
        git(root, "worktree", "prune", check=False)


def show(root: Path, rev: str, path: str) -> str | None:
    """Content of `path` at `rev` (committed), or None if it is not there."""
    proc = subprocess.run(["git", "-C", str(root), "show", f"{rev}:{path}"], capture_output=True, text=True,
                          timeout=GIT_TIMEOUT_S, env=_env(), check=False)
    return proc.stdout if proc.returncode == 0 else None


def show_main(root: Path, path: str) -> str | None:
    """Content of `path` in main (committed, merged), or None if it is not there."""
    return show(root, MAIN, path)


def restart_gateway() -> None:
    """Delayed, in its own transient unit: restarting from inside the gateway would kill this process.

    After the restart the same unit tells the user on Telegram (`hermes send`, or `ultron-hub send` in direct mode; text only, absolute path: the
    unit's PATH is reduced): « ✅ Hermes redémarré » or « ❌ Hermes ne redémarre pas » (todo #140).
    """
    cmd = ["systemd-run", "--user", "--collect", "--quiet", f"--on-active={RESTART_DELAY_S}"]
    from ultron_hub import envoi
    direct = envoi.mode() == "direct"
    # direct mode (todo #197): ultron-bot holds Telegram and loads hermes_plugin; hermes-gateway must stay stopped
    unit, name = ("ultron-bot", "Le bot") if direct else ("hermes-gateway", "Hermes")
    try:
        # direct mode (todo #194): the unit calls `ultron-hub send -t telegram` (same shape as `hermes send`)
        hermes = (shutil.which("ultron-hub") or str(Path.home() / ".local/bin/ultron-hub")) if direct else resolve_hermes(None)
    except Exception:
        hermes = None
    if direct:
        cmd += [f"--setenv={name}" for name in (envoi.TOKEN_ENV, *envoi.CHAT_ENVS, envoi.MODE_ENV)]
    if hermes is None:
        cmd += [SYSTEMCTL, "--user", "restart", unit]
    else:
        ids = merged_ids_safe()
        what = f" (fusion de {', '.join(f'#{t}' for t in ids)})" if ids else ""
        script = (f'{SYSTEMCTL} --user restart {unit}; rc=$?; '
                  'if [ "$rc" -eq 0 ]; then sleep 5; "$1" send -t telegram "$2"; '
                  'else "$1" send -t telegram "$3 (code $rc)"; fi; exit "$rc"')
        cmd += ["/bin/sh", "-c", script, "sh", hermes, f"✅ {name} redémarré{what}", f"❌ {name} ne redémarre pas"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=_env(), check=False)
    if proc.returncode != 0:
        raise UltronHubError(f"redémarrage de {unit} impossible : {proc.stderr.strip()[-300:]}")


DASHBOARD_UNIT = "ultron-hub-dashboard.service"
DASHBOARD_PATH = "src/ultron_hub/dashboard/"


def restart_dashboard() -> None:
    """Delayed restart of the dashboard service in its own transient timer unit (todo #294): the dashboard may be the
    caller of the merge, and a merge that changed its code must not leave the old server running."""
    cmd = ["systemd-run", "--user", "--collect", "--quiet", f"--on-active={RESTART_DELAY_S}",
           SYSTEMCTL, "--user", "restart", DASHBOARD_UNIT]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=_env(), check=False)
    if proc.returncode != 0:
        raise UltronHubError(f"redémarrage de {DASHBOARD_UNIT} impossible : {proc.stderr.strip()[-300:]}")


def dashboard_changed(t: dict) -> bool:
    """Whether the merge of this repository changed the dashboard's code (`before` .. `merge_sha`)."""
    if not (t.get("merged") and t.get("before") and t.get("merge_sha")):
        return False
    names = git(t["path"], "diff", "--name-only", t["before"], t["merge_sha"], check=False)
    return any(n.startswith(DASHBOARD_PATH) for n in names.splitlines())


def merged_ids_safe() -> list[int]:
    try:
        from ultron_hub.common.settings import load_settings
        return merged_ids(load_settings())
    except Exception:
        return []


def _status_files(porcelain: str) -> list[tuple[str, str]]:
    """(path, kind) for each line of `git status --porcelain`; kind is "non suivi" or "modifié"."""
    out = []
    for line in porcelain.splitlines():  # `git()` strips the output: the first line may have lost its leading space
        code, _, path = line.lstrip().partition(" ")
        if path.strip():
            out.append((path.strip().strip('"'), "non suivi" if code == "??" else "modifié"))
    return out


def _files_block(files: list[tuple[str, str]]) -> str:
    lines = [f"• {path}" + (f" ({kind})" if kind else "") for path, kind in files[:MAX_FILES_SHOWN]]
    if len(files) > MAX_FILES_SHOWN:
        lines.append(f"… et {len(files) - MAX_FILES_SHOWN} autres")
    return "\n".join(lines)


def _one_line(text: str) -> str:
    return " ".join(text.split())


def log_refusal(settings: Settings, todo_id: int, step: str, reason: str, files: list[str] = ()) -> None:
    """A refused 🔀 leaves a trace in var/logs/ultron-hub.log and in the esprit journal (names only, never content)."""
    shown = list(files)[:MAX_FILES_SHOWN]
    detail = _one_line(reason + (" : " + ", ".join(shown) if shown else ""))
    log.warning("todo=%d step=%s reason=%s", todo_id, step, detail)
    journal.record(settings, "ultron-hub", "fusion", f"Fusion refusée : {detail}", todo_id)


def refusal_text(todo_id: int, reason: str, todo_hint: str, files: list[tuple[str, str]] = ()) -> str:
    """Telegram text of a refusal: the cause, the files involved (at most 10), what to do."""
    parts = [f"❌ Fusion refusée : {reason}"]
    if files:
        parts.append("Fichiers en cause :\n" + _files_block(list(files)))
    parts += [f"À faire : {todo_hint}", f"La tâche #{todo_id} reste ouverte, main n'a pas changé."]
    return "\n\n".join(parts)


def _refuse(settings: Settings, todo_id: int, step: str, reason: str, todo_hint: str,
            files: list[tuple[str, str]] = ()) -> NoReturn:
    log_refusal(settings, todo_id, step, reason, [path for path, _ in files])
    raise InvalidRequestError(refusal_text(todo_id, reason, todo_hint, files))


@contextmanager
def lock(settings: Settings, todo_id: int | None = None) -> Iterator[None]:
    settings.var_dir.mkdir(parents=True, exist_ok=True)
    with open(settings.var_dir / "merge.lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if todo_id is not None:
                _refuse(settings, todo_id, "lock", "une fusion est déjà en cours",
                        "attends la fin de la fusion en cours (une minute), puis relance 🔀.")
            raise InvalidRequestError("une fusion est déjà en cours") from None
        yield


def merge_and_close(settings: Settings, todo_id: int, restart: Restarter = restart_gateway,
                    by: dict | None = None, progress: Progress | None = None, *, close: bool = True,
                    guard: Callable[[list[dict]], None] | None = None,
                    restart_dash: Restarter | None = None) -> dict:
    """Trial merge + tests in every repository having a todo-<id>/… branch, then merge them all into main,
    remove the branches (and their worktrees), close the todo, restart the gateway if ultron-hub changed.

    The caller checks who asked (`closing.authorize`, done by `ultron-hub branch merge`); `by` is recorded.
    `close=False` (automatic merge, todo #254, `auto_merge.py`): the same path, but the todo stays open, marked
    delivered for the user's ✅. `guard(found)` runs under the lock after the usual checks and may refuse; the head
    of each branch is then pinned (`t["rev"]`): the trial merge and the real one use that exact commit.

    Todo #294: a merge into ultron-hub that touches src/ultron_hub/dashboard/ also plans a delayed restart of the
    dashboard service (`restart_dash`, default `restart_dashboard`; `result["dashboard"]` = "planned" or "échec : …").

    Any failure before the real merges leaves everything as it was: the todo stays open.
    """
    own_progress = progress is None
    progress = progress or Progress(settings, [todo_id])
    with lock(settings, todo_id):
        progress.step(todo_id, "essai")
        sweep_merge_check(settings)
        with TodoStore(settings.todo_db) as todos:
            if todos.get(todo_id).status != "todo":
                _refuse(settings, todo_id, "todo-status", f"la tâche #{todo_id} n'est plus ouverte",
                        "rien à fusionner ; rouvre la tâche si du code reste à intégrer.")
        found = targets(settings, todo_id)
        if not found or not all(t["branch"] for t in found):
            names = [label(settings, t["repo"], b) for t in found for b in t["branches"]]
            reason = (f"aucune branche unique todo-{todo_id}/… par dépôt "
                      f"({len(names)} trouvée(s) : {', '.join(names) or 'aucune'})")
            step = "branches-multiple" if names else "branch-missing"
            _refuse(settings, todo_id, step, reason,
                    f"garde une seule branche todo-{todo_id}/… par dépôt (supprime ou fusionne les autres)."
                    if names else f"applique d'abord le travail sur une branche todo-{todo_id}/… (bouton 🌿).")
        for t in found:
            root, branch = t["path"], t["branch"]
            if t["current"] not in (MAIN, branch):
                why = "son code disparaîtrait du bot" if root == settings.root else "son code serait mis de côté"
                _refuse(settings, todo_id, "wrong-branch",
                        f"{t['repo']} est sur la branche {t['current']} : fusion refusée ({why})",
                        f"repasse {t['repo']} sur main (`git switch main`) ou travaille sur {t['current']} "
                        f"dans un worktree à part, puis relance 🔀.")
            dirty = git(root, "status", "--porcelain", "--untracked-files=no")
            if dirty:
                _refuse(settings, todo_id, "main-dirty",
                        f"{t['repo']} a des modifications non commitées : fusion refusée",
                        "commite ou annule ces modifications (ou demande à Claude Code), puis relance 🔀.",
                        _status_files(dirty))
            t["worktree"] = branch_worktree(root, branch)
            wt_dirty = git(t["worktree"], "status", "--porcelain", check=False) if t["worktree"] else ""
            if wt_dirty:
                files = _status_files(wt_dirty)
                untracked = [p for p, kind in files if kind == "non suivi"]
                hint = (f"fichier non suivi {untracked[0]} dans le worktree : supprime-le ou demande à Claude Code"
                        if len(untracked) == 1 and len(files) == 1 else
                        "commite, annule ou supprime ces fichiers dans le worktree (ou demande à Claude Code), "
                        "puis relance 🔀.")
                _refuse(settings, todo_id, "worktree-dirty",
                        f"le worktree {t['worktree']} a des modifications non commitées ou des fichiers non suivis",
                        hint, files)
        ready = readiness(settings, todo_id, found)
        if not ready["pret"] and any(t["ahead"] for t in found):  # nothing ahead: the todo only closes
            _refuse(settings, todo_id, "pas-prete", f"la tâche #{todo_id} n'est pas prête : {ready['raison']}",
                    f"termine le travail et commite-le avec un message finissant par « (todo #{todo_id}) », "
                    "puis relance 🔀.")
        if guard is not None:
            for t in found:
                t["rev"] = git(t["path"], "rev-parse", "--verify", f"refs/heads/{t['branch']}^{{commit}}")
            guard(found)
        for t in found:
            t["check"] = (trial_merge(settings, t["repo"], t["path"], t.get("rev") or t["branch"],
                                on_tests=lambda: progress.step(todo_id, "tests")) if t["ahead"]
                          else {"ok": True, "stage": None, "detail": "rien à fusionner"})
            t.update(merged=False, cleanup=None)
            if not t["check"]["ok"]:
                break
        failed = next((t for t in found if "check" in t and not t["check"]["ok"]), None)
        result = {"todo_id": todo_id, "branch": ", ".join(label(settings, t["repo"], t["branch"]) for t in found),
                  "ahead": sum(t["ahead"] for t in found),
                  "check": failed["check"] if failed else _combined(settings, found),
                  "merged": False, "closed": False, "restart": None, "cleanup": None, "repos": [],
                  "after_merge": [], "after_merge_error": None, "dashboard": None}
        if failed:
            result["branch"] = label(settings, failed["repo"], failed["branch"])
            check = failed["check"]
            if check["stage"] == "conflict":
                log_refusal(settings, todo_id, "trial-merge", f"conflit avec main sur {result['branch']}",
                            check.get("files") or [])
            else:
                why = (f"les tests dépassent le délai de {repos.TESTS_TIMEOUT_S} s" if check.get("timeout")
                       else "les tests échouent")
                log_refusal(settings, todo_id, "tests", f"{why} sur {result['branch']}")
            return result
        progress.step(todo_id, "fusion")
        for t in found:
            t["before"] = git(t["path"], "rev-parse", MAIN)
            _merge_one(t, todo_id, auto=not close)
        result["merged"] = any(t["merged"] for t in found)
        result["cleanup"] = "; ".join(t["cleanup"] for t in found if t["cleanup"]) or None
        result["repos"] = [{"repo": t["repo"], "branch": t["branch"], "ahead": t["ahead"], "merged": t["merged"],
                            "check": t["check"], "cleanup": t["cleanup"]} for t in found]
        progress.step(todo_id, "cloture")
        if close:
            with TodoStore(settings.todo_db) as todos:
                task = todos.set_status(todo_id, "done", by=by)
            result["closed"] = True
            journal.record(settings, "ultron-hub", "fusion",
                           f"Branche {result['branch']} fusionnée dans main, todo terminée : {task.title}", todo_id)
            result["after_merge"], result["after_merge_error"] = _after_merge(settings, todo_id, found)
        else:  # the guard refused any after-merge declaration: nothing to record
            result["repos"] = [{**r, "sha": t.get("merge_sha")} for r, t in zip(result["repos"], found)]
            shas = ", ".join(t["merge_sha"][:12] for t in found if t.get("merge_sha")) or "rien à fusionner"
            with TodoStore(settings.todo_db) as todos:  # open, delivered: it shows in 📥 À toi for the user's ✅
                todos.mark_delivered(todo_id, by=(by or {}).get("by") or "cli", note=f"fusion automatique {shas}")
            journal.record(settings, "ultron-hub", "fusion", f"Fusion automatique : todo #{todo_id}, {shas}", todo_id)
        progress.done(todo_id)
    if not any(t["path"] == settings.root for t in found):
        return result  # the bot's code did not change
    if any(t["path"] == settings.root and dashboard_changed(t) for t in found):
        try:
            (restart_dash or restart_dashboard)()
            result["dashboard"] = "planned"
        except Exception as exc:
            log.exception("dashboard restart failed")
            result["dashboard"] = f"échec : {exc}"
    if own_progress:
        progress.restart()
    try:
        restart()
        result["restart"] = "planned"
    except Exception as exc:  # merged and closed anyway: report, do not fail
        log.exception("gateway restart failed")
        result["restart"] = f"échec : {exc}"
    return result


def _after_merge(settings: Settings, todo_id: int, found: list[dict]) -> tuple[list[dict], str | None]:
    """Server actions the todo declared for after its merge (ultron-hub only), recorded pending: each one waits
    for its own signed "✅ Exécuter" button. Best effort: the merge is done whatever happens here."""
    if not any(t["path"] == settings.root for t in found):
        return [], None
    try:
        return after_merge.record(settings, todo_id, lambda path: show_main(settings.root, path)), None
    except Exception as exc:
        log.exception("todo=%d after-merge declaration", todo_id)
        return [], str(exc)


def _combined(settings: Settings, found: list[dict]) -> dict:
    if len(found) == 1:
        return found[0]["check"]
    detail = "\n".join(f"{t['repo']} : {t['check']['detail']}" for t in found)
    return {"ok": True, "stage": None, "detail": detail, "tested": all(t["check"].get("tested", True) for t in found)}


AUTO_MESSAGE = "Fusion automatique de {branch} (todo #{todo_id}, reste ouverte)"


def _merge_one(t: dict, todo_id: int, auto: bool = False) -> None:
    root, branch = t["path"], t["branch"]
    if t["ahead"]:
        git(root, "switch", MAIN)
        message = (AUTO_MESSAGE if auto else "Fusion de {branch} (todo #{todo_id} terminée)").format(
            branch=branch, todo_id=todo_id)
        try:
            git(root, "merge", "--no-ff", t.get("rev") or branch, "-m", message)
        except UltronHubError:
            git(root, "merge", "--abort", check=False)
            git(root, "switch", t["current"], check=False)
            raise
        t["merged"] = True
        t["merge_sha"] = git(root, "rev-parse", MAIN)
        log.warning("todo=%d branch %s of %s merged into %s", todo_id, branch, t["repo"], MAIN)
    else:
        git(root, "switch", MAIN)
    # The branch is in main now: a cleanup failure must not leave the todo open (seen on 2026-10-11).
    try:
        if t["worktree"]:
            git(root, "worktree", "remove", str(t["worktree"]))
        git(root, "branch", "-d", branch)
    except UltronHubError as exc:
        log.warning("todo=%d cleanup of %s failed: %s", todo_id, branch, exc)
        t["cleanup"] = str(exc)
