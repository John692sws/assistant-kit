"""The 20 checks of the tidiness scan. Each takes a `Scan` and returns a list of anomalies:

    {"check": n, "kind": str, "path": str, "detail": str, "action": str}

`action` is only a proposal (a command or a gesture for the user); nothing here changes the disk.
"""

import fnmatch
import glob
import hashlib
import json
import os
import re
import shlex
import sqlite3
from pathlib import Path
from typing import Any

from ultron_hub.menage.rules import BudgetExceeded, Scan, iter_tree, scandir, tree_size

MB = 1024 * 1024
# A growth in percent alone is noise on tiny folders: it also needs this many bytes.
MIN_GROWTH_BYTES = 10 * MB
HASH_BUDGET_BYTES = 8 * 1024 * MB


def anomaly(check: int, kind: str, path: str, detail: str, action: str) -> dict[str, Any]:
    return {"check": check, "kind": kind, "path": path, "detail": detail, "action": action}


def _age_text(days: float) -> str:
    return f"{int(days)} j"


def _is_empty_tree(path: str) -> bool:
    for _root, dirs, files in os.walk(path, followlinks=False):
        if files or any(os.path.islink(os.path.join(_root, d)) for d in dirs):
            return False
    return True


# --- A. Structure -----------------------------------------------------------------------------------------------

def check_1_expected(scan: Scan) -> list[dict]:
    out = []
    for raw in scan.cfg["structure"].get("expected", []):
        path = scan.p(raw)
        if not os.path.isdir(path):
            out.append(anomaly(1, "missing_dir", path, "dossier attendu absent", f"mkdir -p {path}"))
    return out


def check_2_3_root_layout(scan: Scan) -> list[dict]:
    """Undeclared directories (2), stray files (3), obsolete and abandoned items."""
    structure = scan.cfg["structure"]
    out = []
    obsolete = {scan.p(x) for x in structure.get("obsolete", [])}
    for path in sorted(obsolete):
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        if _is_empty_tree(path):
            out.append(anomaly(2, "obsolete_empty", path, "vides, à retirer ?", f"supprimer le dossier vide {path}  (à confirmer)"))
        else:
            out.append(anomaly(2, "obsolete_not_empty", path, "dossier obsolète non vide, non déclaré",
                               "déplacer le contenu vers sa place (voir config/menage.yaml) puis retirer le dossier"))
    for item in structure.get("abandoned", []):
        path = scan.p(item["path"])
        if os.path.lexists(path):
            out.append(anomaly(2, "abandoned", path, f"abandonné, à archiver ? ({item.get('reason', '')})".strip(),
                               f"déplacer vers {scan.home}/workspace/archive/"))
    for raw, rule in structure.get("allowed", {}).items():
        base = scan.p(raw)
        if not os.path.isdir(base):
            continue
        dirs = list(rule.get("dirs", [])) + list(rule.get("dotdirs", []))
        files = list(rule.get("files", []))
        for entry in scandir(scan, base):
            if scan.is_excluded(entry.path) or entry.path in obsolete:
                continue
            try:
                is_dir = entry.is_dir()  # a link to a directory counts as a directory
            except OSError:
                continue
            names = dirs if is_dir else files
            if any(fnmatch.fnmatchcase(entry.name, pat) for pat in names):
                continue
            if is_dir:
                out.append(anomaly(2, "undeclared_dir", entry.path, f"dossier non déclaré dans {base}",
                                   "le ranger (voir config/menage.yaml) ou le déclarer"))
            else:
                out.append(anomaly(3, "stray_file", entry.path, f"fichier en vrac dans {base}",
                                   "le ranger dans son dossier ou le jeter"))
    return out


def _git_head_time(project: str) -> float | None:
    """Time of the last HEAD movement, read from `.git/logs/HEAD` (no git process)."""
    log = os.path.join(project, ".git", "logs", "HEAD")
    try:
        with open(log, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 4096))
            lines = handle.read().decode("utf-8", "replace").strip().splitlines()
        if not lines:
            return None
        head = lines[-1].split("\t", 1)[0].split()
        return float(head[-2])  # "<old> <new> <name> <email> <ts> <tz>"
    except (OSError, ValueError, IndexError):
        return None


def _newest_mtime(scan: Scan, project: str, stop_at: float) -> float:
    newest = 0.0
    for entry in iter_tree(scan, project):
        try:
            if entry.is_file(follow_symlinks=False):
                newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
        except OSError:
            continue
        if newest >= stop_at:
            break
    return newest


def check_4_inactive_projects(scan: Scan) -> list[dict]:
    root = scan.p(scan.cfg["worktrees"]["projects_root"])
    days = scan.cfg["structure"].get("inactive_project_days")
    if not days or not os.path.isdir(root):
        return []
    cutoff = scan.now - days * 86400
    out = []
    for entry in scandir(scan, root):
        if entry.name.endswith(".worktrees") or entry.name.startswith(".") or entry.is_symlink() \
                or not entry.is_dir(follow_symlinks=False) or scan.is_excluded(entry.path):
            continue
        last = _git_head_time(entry.path) or 0.0
        if last < cutoff:
            last = max(last, _newest_mtime(scan, entry.path, cutoff))
        if last < cutoff:
            if not last:
                last = entry.stat(follow_symlinks=False).st_mtime
            out.append(anomaly(4, "inactive_project", entry.path,
                               f"aucune modification depuis {_age_text(scan.age_days(last))}",
                               f"mv {entry.path} {scan.home}/workspace/projects/paused/  (à confirmer)"))
    return out


# --- B. Worktrees -----------------------------------------------------------------------------------------------

def _repos(scan: Scan) -> list[str]:
    root = scan.p(scan.cfg["worktrees"]["projects_root"])
    return [e.path for e in scandir(scan, root)
            if not e.is_symlink() and os.path.isdir(os.path.join(e.path, ".git"))]


def _admin_gitdir_mtimes(scan: Scan) -> dict[str, float]:
    """Worktree path -> mtime of its `.git/worktrees/<name>/gitdir` record."""
    result = {}
    for repo in _repos(scan):
        base = os.path.join(repo, ".git", "worktrees")
        for entry in scandir(scan, base) if os.path.isdir(base) else []:
            record = os.path.join(entry.path, "gitdir")
            try:
                text = Path(record).read_text(encoding="utf-8").strip()
                result[os.path.normpath(text[:-5] if text.endswith("/.git") else text)] = os.lstat(record).st_mtime
            except OSError:
                continue
    return result


def list_worktrees(scan: Scan) -> list[tuple[str, str]]:
    """(repo, worktree path) from `.git/worktrees/*/gitdir`; the main checkout is not listed."""
    found = []
    for repo in _repos(scan):
        base = os.path.join(repo, ".git", "worktrees")
        if not os.path.isdir(base):
            continue
        for entry in scandir(scan, base):
            try:
                text = Path(entry.path, "gitdir").read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if text.endswith("/.git"):
                text = text[:-5]
            found.append((repo, os.path.normpath(text)))
    return found


def check_5_worktree_convention(scan: Scan) -> list[dict]:
    root = scan.p(scan.cfg["worktrees"]["projects_root"])
    pattern = re.compile(scan.cfg["worktrees"]["pattern"])
    merge_check = scan.p(scan.cfg["worktrees"]["merge_check"]["path"])  # belongs to check 7
    out = []
    for repo, wt in list_worktrees(scan):
        if wt.startswith(merge_check + "/"):
            continue
        rel = os.path.relpath(wt, root) if wt.startswith(root + "/") else None
        match = pattern.fullmatch(rel) if rel else None
        if not match or match.group("projet") != os.path.basename(repo):
            out.append(anomaly(5, "worktree_off_convention", wt,
                               f"worktree de {os.path.basename(repo)} hors de {scan.cfg['worktrees']['convention']}",
                               f"git -C {repo} worktree move {wt} <{os.path.basename(repo)}.worktrees/todo-ID>"))
        elif not os.path.isdir(wt):
            out.append(anomaly(5, "worktree_missing", wt, "dossier du worktree absent",
                               f"git -C {repo} worktree prune"))
    return out


def _todo_status(scan: Scan) -> dict[int, str] | None:
    db = scan.todo_db
    if db is None or not Path(db).is_file():
        scan.warn("base des todos introuvable : vérification 6 ignorée")
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
        try:
            return {int(i): s for i, s in conn.execute("SELECT id, status FROM tasks")}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        scan.warn(f"base des todos illisible ({type(exc).__name__}) : vérification 6 ignorée")
        return None


def _branches(repo: str) -> set[str]:
    names: set[str] = set()
    heads = os.path.join(repo, ".git", "refs", "heads")
    for root, _dirs, files in os.walk(heads, followlinks=False):
        for name in files:
            names.add(os.path.relpath(os.path.join(root, name), heads))
    try:
        for line in Path(repo, ".git", "packed-refs").read_text(encoding="utf-8").splitlines():
            if " refs/heads/" in line and not line.startswith(("#", "^")):
                names.add(line.split(" refs/heads/", 1)[1].strip())
    except OSError:
        pass
    return names


def check_6_closed_todo_leftovers(scan: Scan) -> list[dict]:
    status = _todo_status(scan)
    if status is None:
        return []
    branch_re = re.compile(scan.cfg["worktrees"]["branch_pattern"])
    out = []
    for repo in _repos(scan):
        for name in sorted(_branches(repo)):
            m = branch_re.fullmatch(name)
            if m and status.get(int(m.group("id")), "todo") != "todo":
                out.append(anomaly(6, "closed_todo_branch", f"{repo}#{name}",
                                   f"branche de la todo #{m.group('id')} (close)",
                                   f"git -C {repo} branch -d {name}  (après vérification de la fusion)"))
    for repo, wt in list_worktrees(scan):
        m = re.fullmatch(r"todo-(\d+)", os.path.basename(wt))
        if m and status.get(int(m.group(1)), "todo") != "todo":
            out.append(anomaly(6, "closed_todo_worktree", wt, f"worktree de la todo #{m.group(1)} (close)",
                               f"git -C {repo} worktree remove {wt}  (après vérification)"))
    return out


def check_7_merge_check(scan: Scan) -> list[dict]:
    cfg = scan.cfg["worktrees"]["merge_check"]
    path = scan.p(cfg["path"])
    if not os.path.isdir(path):
        return []
    limit = cfg["max_age_hours"] / 24.0
    admin = _admin_gitdir_mtimes(scan)
    out = []
    for entry in scandir(scan, path):
        try:
            # Oldest of: the folder's mtime, its `.git` (a file for a worktree) and git's own `gitdir` record of
            # the worktree, written once when it was created (the folder's mtime moves with every checkout).
            times = [entry.stat(follow_symlinks=False).st_mtime]
            if entry.path in admin:
                times.append(admin[entry.path])
            try:
                times.append(os.lstat(os.path.join(entry.path, ".git")).st_mtime)
            except OSError:
                pass
            age = scan.age_days(min(times))
        except OSError:
            continue
        if age > limit:
            out.append(anomaly(7, "stale_merge_check", entry.path,
                               f"reste d'une fusion d'essai vieux de {age * 24:.0f} h",
                               f"git worktree prune, puis supprimer {entry.path}  (après vérification)"))
    return out


# --- C. Disposable ----------------------------------------------------------------------------------------------

def _roots(scan: Scan, raws: list[str]) -> list[str]:
    seen, out = set(), []
    for raw in raws:
        path = scan.p(raw)
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def check_8_disposable_patterns(scan: Scan) -> list[dict]:
    patterns = scan.cfg["disposable"].get("patterns", [])
    out, seen = [], set()
    for root in _roots(scan, scan.cfg["disposable"]["roots"]):
        for entry in iter_tree(scan, root):
            if entry.path in seen:
                continue
            for rule in patterns:
                if not fnmatch.fnmatchcase(entry.name, rule["glob"]):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    break
                age = scan.age_days(st.st_mtime)
                if age >= rule.get("min_age_days", 0):
                    seen.add(entry.path)
                    out.append(anomaly(8, "disposable", entry.path,
                                       f"motif {rule['glob']}, {_age_text(age)}, {st.st_size // 1024} Ko",
                                       f"supprimer {entry.path}  (à confirmer)"))
                break
    return out


def check_9_pycache(scan: Scan) -> list[dict]:
    if not scan.cfg["disposable"].get("pycache"):
        return []
    out, seen = [], set()
    for root in _roots(scan, scan.cfg["disposable"]["roots"]):
        for entry in iter_tree(scan, root):
            if entry.name == "__pycache__" and entry.path not in seen and entry.is_dir(follow_symlinks=False):
                seen.add(entry.path)
                if not _pycache_orphan(scan, entry.path):
                    continue
                out.append(anomaly(9, "pycache", entry.path,
                                   "__pycache__ orphelin (aucune source .py à côté, régénéré à l'import)",
                                   f"supprimer {entry.path}"))
    return out


# `mod.cpython-312.pyc`, pytest's `conftest.cpython-312-pytest-9.1.1.pyc`, and `ultron-privcpython-312.pyc`
# for a script without extension (no dot before the tag)
_PYC_TAG = re.compile(r"^(?P<stem>.+?)\.?(?:cpython|pypy)-\d+.*\.pyc$")


def _pycache_orphan(scan: Scan, path: str) -> bool:
    """True when no `<name>.<tag>.pyc` of the folder has its `<name>.py` in the parent folder."""
    parent = os.path.dirname(path)
    for entry in scandir(scan, path):
        if not entry.name.endswith(".pyc"):
            continue
        match = _PYC_TAG.match(entry.name)
        stem = match["stem"] if match else entry.name[:-4]
        # `<name>.py`, or a script without extension loaded by SourceFileLoader (`ultron-priv`)
        if any(os.path.isfile(os.path.join(parent, name)) for name in (stem + ".py", stem)):
            return False
    return True


def check_10_aged_dirs(scan: Scan) -> list[dict]:
    out = []
    for item in scan.cfg["disposable"].get("aged_dirs", []):
        path = scan.p(item["path"])
        if not os.path.isdir(path):
            continue
        old = []
        sub = item.get("sous_dossier")
        for entry in scandir(scan, path):
            try:
                # todo #464 : `sous_dossier` = on ne vise que <entrée>/<sous_dossier> (ex. work/ d'une vieille mission), le reste est gardé.
                target = os.path.join(entry.path, sub) if sub else entry.path
                if sub and not os.path.isdir(target):
                    continue
                age = scan.age_days(os.stat(target, follow_symlinks=False).st_mtime)
            except OSError:
                continue
            # todo #381 : `traite_si` = fichiers marqueurs ; une entrée sans aucun d'eux n'est pas traitée, on n'y touche pas.
            marks = item.get("traite_si")
            if marks and not (entry.is_dir(follow_symlinks=False)
                              and any(os.path.isfile(os.path.join(entry.path, m)) for m in marks)):
                continue
            if age > item["max_age_days"]:
                old.append((age, f"{entry.name}/{sub}" if sub else entry.name))
        if old:
            old.sort(reverse=True)
            names = ", ".join(f"{n} ({_age_text(a)})" for a, n in old[:5])
            more = f" et {len(old) - 5} autre(s)" if len(old) > 5 else ""
            if sub:  # todo #464 : taille libérable, request/result/compte rendu gardés
                mb = sum(os.path.getsize(os.path.join(r, f)) for _, n in old
                         for r, _, fs in os.walk(os.path.join(path, n)) for f in fs
                         if os.path.isfile(os.path.join(r, f)) and not os.path.islink(os.path.join(r, f))) / 1e6
                more += f" ; {mb:.0f} Mo libérables, le reste de chaque entrée est gardé"
            out.append(anomaly(10, "aged_entries", path,
                               f"{len(old)} entrée(s){' traitée(s)' if item.get('traite_si') else ''} de plus de {item['max_age_days']} j : {names}{more}",
                               f"purger {'les ' + sub + '/ des entrées' if sub else 'les entrées'} de {path} plus vieilles que {item['max_age_days']} j"))
            out[-1]["count"] = len(old)
    return out


def check_11_logs(scan: Scan) -> list[dict]:
    cfg = scan.cfg["disposable"]["logs"]
    files: list[str] = []
    for raw in cfg.get("dirs", []):
        files += [e.path for e in iter_tree(scan, scan.p(raw), ignore=set()) if e.is_file(follow_symlinks=False)]
    files += [scan.p(raw) for raw in cfg.get("files", [])]
    out, seen = [], set()
    for path in files:
        if path in seen or not os.path.isfile(path) or os.path.islink(path) or scan.is_excluded(path):
            continue
        seen.add(path)
        try:
            st = os.stat(path)
        except OSError:
            continue
        age, size_mb = scan.age_days(st.st_mtime), st.st_size / MB
        if size_mb > cfg["max_size_mb"]:
            out.append(anomaly(11, "big_log", path, f"journal de {size_mb:.0f} Mo (seuil {cfg['max_size_mb']} Mo)",
                               "faire tourner ou purger ce journal"))
        elif age > cfg["max_age_days"]:
            out.append(anomaly(11, "old_log", path, f"journal sans écriture depuis {_age_text(age)}",
                               "archiver ou supprimer ce journal"))
    return out


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_12_duplicates(scan: Scan) -> list[dict]:
    cfg = scan.cfg["disposable"]["duplicates"]
    min_size = cfg["min_size_mb"] * MB
    skip = [scan.p(x) for x in cfg.get("skip", [])]
    by_size: dict[int, list[str]] = {}
    seen_files, seen_inodes = set(), set()
    for root in _roots(scan, cfg["roots"]):
        for entry in iter_tree(scan, root):
            path = entry.path
            if path in seen_files or any(path == s or path.startswith(s + "/") for s in skip) \
                    or scan.is_secret(path):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if st.st_size < min_size or (st.st_dev, st.st_ino) in seen_inodes:
                continue
            seen_files.add(path)
            seen_inodes.add((st.st_dev, st.st_ino))
            by_size.setdefault(st.st_size, []).append(path)
    out, hashed = [], 0
    for size, paths in sorted(by_size.items(), reverse=True):
        if len(paths) < 2:
            continue
        groups: dict[str, list[str]] = {}
        for path in paths:
            if hashed + size > HASH_BUDGET_BYTES:
                scan.warn("doublons : budget de lecture épuisé, résultats partiels")
                return out
            try:
                groups.setdefault(_sha256(path), []).append(path)
                hashed += size
            except OSError as exc:
                scan.warn(f"lecture impossible : {path} ({type(exc).__name__})")
        for same in groups.values():
            if len(same) > 1:
                others = ", ".join(same[1:])
                out.append(anomaly(12, "duplicate", same[0], f"{size / MB:.1f} Mo, identique à : {others}",
                                   "garder un seul exemplaire"))
    return out


# --- D. Space ---------------------------------------------------------------------------------------------------

def check_13_disk(scan: Scan) -> list[dict]:
    cfg = scan.cfg["space"]
    out = []
    for disk in (scan.probe() if scan.probe else []):
        pct = disk["used_pct"]
        if pct >= cfg["disk_crit_percent"]:
            kind = "disk_crit"
        elif pct >= cfg["disk_warn_percent"]:
            kind = "disk_warn"
        else:
            continue
        out.append(anomaly(13, kind, disk["mount"], f"disque rempli à {pct} % ({disk['free_gib']} Gio libres)",
                           "libérer de la place (voir les autres anomalies)"))
    return out


def _last_history(scan: Scan) -> dict[str, int]:
    """`sizes` of the last line of history.jsonl ({"ts": ..., "sizes": {path: bytes}}); empty if none."""
    path = scan.history_path
    if path is None or not Path(path).is_file():
        return {}
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 262144))
            lines = [x for x in handle.read().decode("utf-8", "replace").splitlines() if x.strip()]
        for line in reversed(lines):
            try:
                sizes = json.loads(line).get("sizes")
            except (ValueError, AttributeError):
                continue
            if isinstance(sizes, dict):
                return {str(k): int(v) for k, v in sizes.items() if isinstance(v, (int, float))}
    except OSError as exc:
        scan.warn(f"historique illisible ({type(exc).__name__})")
    return {}


def _measure(scan: Scan, raws: list[str], cache: dict[str, int]) -> None:
    for raw in raws:
        path = scan.p(raw)
        if path in scan.sizes or scan.is_excluded(path) or not os.path.isdir(path) or os.path.islink(path):
            continue
        try:
            scan.sizes[path] = tree_size(scan, path, cache)
        except BudgetExceeded:
            scan.warn(f"taille de {path} non mesurée (budget épuisé)")


def check_14_growth(scan: Scan) -> list[dict]:
    cfg = scan.cfg["space"]
    _measure(scan, cfg.get("watched", []), scan.size_cache)
    previous = _last_history(scan)
    watched = {scan.p(x) for x in cfg.get("watched", [])}
    out = []
    for path, size in list(scan.sizes.items()):
        if path not in previous or path not in watched:
            continue
        before = previous[path]
        delta = size - before
        pct = 100.0 * delta / before if before > 0 else 0.0
        if delta >= cfg["growth"]["mb"] * MB or (pct >= cfg["growth"]["pct"] and delta >= MIN_GROWTH_BYTES):
            out.append(anomaly(14, "growth", path,
                               f"+{delta / MB:.0f} Mo (+{pct:.0f} %) depuis le dernier passage : "
                               f"{before / MB:.0f} -> {size / MB:.0f} Mo",
                               f"chercher ce qui grossit : du -h --max-depth=1 {path} | sort -h"))
    return out


def check_15_caches(scan: Scan) -> list[dict]:
    out = []
    for cache in scan.cfg["space"].get("caches", []):
        path = scan.p(cache["path"])
        _measure(scan, [cache["path"]], scan.size_cache)
        size = scan.sizes.get(path)
        if size is not None and size > cache["max_mb"] * MB:
            out.append(anomaly(15, "cache_big", path,
                               f"cache {cache['name']} de {size / MB:.0f} Mo (seuil {cache['max_mb']} Mo)",
                               cache["purge"]))
    return out


# --- E. Misplaced code ------------------------------------------------------------------------------------------

def _in_repo(path: str, cache: dict[str, bool]) -> bool:
    chain, current, found = [], path, False
    while True:
        if current in cache:
            found = cache[current]
            break
        chain.append(current)
        if os.path.exists(os.path.join(current, ".git")):
            found = True
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for item in chain:
        cache[item] = found
    return found


def check_16_untracked_tests(scan: Scan) -> list[dict]:
    candidates: set[str] = set()
    has_git = lambda p: os.path.exists(os.path.join(p, ".git"))  # noqa: E731
    for raw in scan.cfg["code"]["roots"]:
        root = scan.p(raw)
        if not os.path.isdir(root):
            continue
        if root == scan.home:  # home: itself and its direct children only
            level = [root] + [e.path for e in scandir(scan, root)
                              if not e.is_symlink() and e.is_dir(follow_symlinks=False)
                              and not scan.is_excluded(e.path) and e.name not in scan.ignore_dirs]
            for directory in level:
                for entry in scandir(scan, directory):
                    if scan.is_excluded(entry.path) or entry.is_symlink():
                        continue
                    if (entry.name == "tests" and entry.is_dir(follow_symlinks=False)) or \
                            (entry.is_file(follow_symlinks=False) and fnmatch.fnmatchcase(entry.name, "test_*.py")):
                        candidates.add(directory)
            continue
        for entry in iter_tree(scan, root, descend=lambda p: not has_git(p)):
            parent = os.path.dirname(entry.path)
            if entry.name == "tests" and entry.is_dir(follow_symlinks=False):
                candidates.add(parent)
            elif fnmatch.fnmatchcase(entry.name, "test_*.py") and entry.is_file(follow_symlinks=False):
                candidates.add(parent)
    cache: dict[str, bool] = {}
    out = []
    for directory in sorted(candidates):
        if any(directory.startswith(c + "/") for c in candidates):  # only the topmost folder is reported
            continue
        if not _in_repo(directory, cache):
            out.append(anomaly(16, "tests_outside_repo", directory, "code testé hors de tout dépôt git",
                               f"le déplacer dans un dépôt de {scan.cfg['code']['repos_root']}"))
    return out


_UNIT_PATH = re.compile(r"^(?:/|~|%h)")


def _exec_words(unit_text: str) -> list[str]:
    words: list[str] = []
    lines = unit_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line.startswith("ExecStart="):
            continue
        value = line[len("ExecStart="):]
        while value.endswith("\\") and i < len(lines):
            value = value[:-1] + " " + lines[i].strip()
            i += 1
        value = value.lstrip("-@+!: ")
        try:
            tokens = shlex.split(value)
        except ValueError:
            tokens = value.split()
        words += [t for t in tokens if _UNIT_PATH.match(t)]
    return words


def check_17_exec_prefixes(scan: Scan) -> list[dict]:
    cfg = scan.cfg["code"]
    base = scan.p(cfg["systemd_user_dir"])
    prefixes = [scan.p(x).rstrip("/") + "/" for x in cfg["exec_allowed_prefixes"]]
    out = []
    for entry in scandir(scan, base):
        if not entry.name.endswith(".service"):
            continue
        try:
            text = Path(entry.path).read_text(encoding="utf-8", errors="replace")  # follows the link
        except OSError as exc:
            scan.warn(f"unité illisible : {entry.path} ({type(exc).__name__})")
            continue
        for word in _exec_words(text):
            expanded = os.path.normpath(word.replace("%h", scan.home, 1) if word.startswith("%h")
                                        else scan.p(word))
            if not any(expanded.startswith(p) for p in prefixes):
                out.append(anomaly(17, "exec_outside_allowed", entry.path,
                                   f"ExecStart utilise {word}, hors des préfixes permis",
                                   "déplacer ce fichier sous ~/workspace/projects/active ou ~/.local/bin, "
                                   "ou déclarer le préfixe"))
                out[-1]["target"] = word
    return out


def _fingerprints(scan: Scan, root: str) -> dict[str, tuple[int, str]]:
    result = {}
    for entry in iter_tree(scan, root, ignore={"__pycache__"}):
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            size = entry.stat(follow_symlinks=False).st_size
            digest = "" if scan.is_secret(entry.path) else _sha256(entry.path)  # secrets: name and size only
        except OSError as exc:
            scan.warn(f"lecture impossible : {entry.path} ({type(exc).__name__})")
            continue
        result[os.path.relpath(entry.path, root)] = (size, digest)
    return result


def check_18_plugins(scan: Scan) -> list[dict]:
    cfg = scan.cfg["code"]
    plugins = scan.p(cfg["hermes_plugins_dir"])
    out = []
    for name, raw_source in cfg.get("plugin_sources", {}).items():
        path, source = os.path.join(plugins, name), scan.p(raw_source)
        if not os.path.lexists(path) or os.path.islink(path):
            continue
        if not os.path.isdir(source):
            scan.warn(f"source du plugin {name} introuvable : {source}")
            continue
        mine, theirs = _fingerprints(scan, path), _fingerprints(scan, source)
        diff = sorted(set(mine) ^ set(theirs) | {k for k in set(mine) & set(theirs) if mine[k] != theirs[k]})
        if diff:
            shown = ", ".join(diff[:5]) + (f" et {len(diff) - 5} autre(s)" if len(diff) > 5 else "")
            out.append(anomaly(18, "plugin_copy_differs", path,
                               f"copie (pas un lien) qui diffère de {source} : {shown}",
                               f"remplacer la copie par un lien vers {source}  (après avoir vérifié la différence)"))
    return out


def check_19_living_files(scan: Scan) -> list[dict]:
    """Living files (read at every session: CLAUDE.md, Reprise, chantier sheets, memory index) over their size budget (todo #168)."""
    out = []
    budgets = scan.cfg.get("budgets") or {}
    actions = {scan.p(k): v for k, v in (budgets.get("actions") or {}).items()}
    limits: dict[str, float] = {}  # a file matched by several patterns keeps the smallest budget, reported once
    for pattern, max_kb in budgets.get("files", {}).items():
        for path in glob.glob(scan.p(pattern)):
            if not os.path.islink(path) and os.path.isfile(path):
                limits[path] = min(float(max_kb), limits.get(path, float(max_kb)))
    for path, max_kb in sorted(limits.items()):
        size_kb = os.lstat(path).st_size / 1024
        if size_kb > max_kb:
            out.append(anomaly(19, "living_file_too_big", path, f"{size_kb:.0f} Ko pour un budget de {max_kb:g} Ko",
                               actions.get(path, "ranger l'historique dans archives/ ou docs/ (garder ici l'état et les règles)")))
    return out


def check_20_squad_subjects(scan: Scan) -> list[dict]:
    """Escouade subjects (~/notes/prompt/*.md) that cannot be assembled: they would burn a signed proof at launch (todo #216)."""
    from ultron_hub.escouade import core as escouade  # lazy: the checks otherwise need nothing from the squads

    folder = Path(scan.p("~/notes/prompt"))
    return [anomaly(20, "squad_subject_incomplete", str(folder / f"{name}.md"), error,
                    "compléter le fichier du sujet (section « ## 4. » notamment) ou le retirer")
            for name, error in escouade.incomplets(folder).items()]


CHECKS = [
    check_1_expected, check_2_3_root_layout, check_4_inactive_projects,
    check_5_worktree_convention, check_6_closed_todo_leftovers, check_7_merge_check,
    check_8_disposable_patterns, check_9_pycache, check_10_aged_dirs, check_11_logs, check_12_duplicates,
    check_13_disk, check_14_growth, check_15_caches,
    check_16_untracked_tests, check_17_exec_prefixes, check_18_plugins, check_19_living_files, check_20_squad_subjects,
]
