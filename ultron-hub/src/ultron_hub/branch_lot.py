"""🔀 Tout fusionner (todo #128): every ready todo merged one after the other, on one signed button.

A todo is ready when it is open and has, in each repository, one `todo-<id>/…` branch, at least one commit ahead of
main in all, at least one clean commit (in no other todo's branch) whose message ends with « (todo #<id>) »
(`branches.readiness`, todo #286: otherwise the todo is « en cours », absent from the lot), and a clean worktree
(untracked files count). Stacked branches (a branch built on another todo's
branch: they share commits not in main, found with merge-base) are merged parent first (when both went on after the fork, the
parent is the todo the shared commits are tagged with, todo #467); a branch stacked on a todo
that is not in the lot is left out, otherwise it would bring that todo's code into main.

Order without conflict (todo #213). Between the todos that are ready, `lot` simulates the chain of merges on main with
`git merge-tree --write-tree` (read only: no ref, no worktree, only unreachable temporary objects), each merge result
standing as a temporary commit made with `git commit-tree`. It keeps the largest set of todos that merge in a chain
without any conflict (parents always before children, then by todo number): every subset is tried when there are 8
todos or fewer (largest first, the lowest todo numbers first on a tie), otherwise a greedy pass takes the todos with
the fewest conflicts first (then the lowest number). A todo that conflicts with the lot is left out ("conflit avec #X
(fichier) : à rebaser après la fusion"), and so are the todos stacked on it. The fingerprint covers the final lot only.

The lot is signed on its fingerprint (`empreinte`): 16 hex of the SHA-256 of "<id>:<repo>:<branch>:<head sha>" lines,
sorted by todo then repository. A branch that moves after the card was shown changes it: refused, nothing merged.
Each todo then goes through `branches.merge_and_close` (trial merge, tests, merge, close). A todo refused by its tests
is set aside and tried again after the others, in successive passes while a pass merges something (todo #378: the
branch that repairs a red main may come last), its stacked children wait for it; any other refusal stops the lot (the
next todos stay open) and the gateway is restarted once, at the end, if ultron-hub's code changed.

After-merge actions (todo #182). `lot` reads each todo's `deploy/user/apres-fusion/todo-<id>.json` at its branch head
(`git show <sha>:…`), validates it with ultron-user (`after_merge.parse`, exact preview) and returns it with the todo;
the card shows those exact commands. The fingerprint also covers every declared action (valid or not): one line
`<id>:action:<n>:<action json>:<args json>:<commands json>:<status>` each, so an action (or its preview) that changes
between the card and the press changes it: refused, nothing merged. Only when EVERY merge of the lot succeeded, the
actions recorded from main (`after_merge.record`, as for one 🔀) are launched through `after_merge.decide` (the path of
"✅ Exécuter", one unit each, proof = the lot signature's start + ":<n>"), and only if: the todo's declaration on main
is the one of the card (same n, action, args), the action was valid on the card and still is with the same commands,
and ultron-user itself did not change during the lot. Anything else stays pending with its own 🧰 button; an invalid
action never runs. `user_unit_restart hermes-gateway` is folded into the lot's single delayed restart (marked done once
that restart is planned). A refusal in the middle of the lot launches nothing.

Report (todo #401). The card shows, in merge order, each todo with its branch, commits and files touched (`main...sha`),
the todos left out and why (the real reason first: `check` removes every todo the signature does not name), and what
a failure does. `run` returns `bilan`: every todo of the lot, merged or open with its reason; `lot_text` starts with it
(the dashboard cuts long texts), and the same line goes to the log and the journal, also when the lot stopped.
"""

import hashlib
import itertools
import json
import re
import subprocess
from pathlib import Path
from typing import Callable, Iterable

from ultron_hub import after_merge, branches, repos, user_actions
from ultron_hub.merge_progress import Progress
from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import Settings
from ultron_hub.esprit import store as journal
from ultron_hub.todo.store import TodoStore

log = get_logger("branch_lot")

IDS_RE = re.compile(r"[0-9]{1,9}(?:,[0-9]{1,9}){0,99}")
EMPREINTE_RE = re.compile(r"[0-9a-f]{16}")
MAIN = branches.MAIN


def parse_ids(text: str) -> list[int]:
    if IDS_RE.fullmatch(text or "") is None:
        raise InvalidRequestError("liste de todos invalide (attendu : 81,82,83)")
    return sorted({int(x) for x in text.split(",")})


def _count(path: Path, *revs: str) -> int:
    out = branches.git(path, "rev-list", "--count", *revs, check=False)
    return int(out) if out.isdigit() else 0


def _all_branches(settings: Settings) -> list[dict]:
    """Every todo-<id>/… branch of every repository: todo_id, repo, path, branch, sha, ahead."""
    found = []
    for name, path in repos.list_repos(settings).items():
        refs = branches.git(path, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/",
                            check=False)
        for line in refs.splitlines():
            ref, _, sha = line.partition(" ")
            if m := branches.BRANCH_RE.fullmatch(ref):
                found.append({"todo_id": int(m[1]), "repo": name, "path": path, "branch": ref, "sha": sha,
                              "ahead": _count(path, ref, f"^{MAIN}")})
    return found


def _parent(a: dict, b: dict) -> dict | None:
    """Which of two branches of one repository the other is stacked on (None: independent)."""
    if not (a["ahead"] and b["ahead"]):
        return None
    base = branches.git(a["path"], "merge-base", a["branch"], b["branch"], check=False)
    if not base or _count(a["path"], base, f"^{MAIN}") == 0:
        return None  # no shared work beyond main
    if _count(a["path"], a["branch"], f"^{b['branch']}") == 0:
        if _count(a["path"], b["branch"], f"^{a['branch']}") == 0:  # same head (todo #286): the one with the work
            mine, theirs = branches.tagged_own_commits([a]), branches.tagged_own_commits([b])
            if theirs and not mine:
                return b
        return a  # a is contained in b
    if _count(a["path"], b["branch"], f"^{a['branch']}") == 0:
        return b
    # Both went on after the fork. The shared commits tell whose work it was: the other one was stacked on an old head
    # of it (todo #467, lot of 28/10: #447 forked from an old tip of todo-458, then put first as the shorter one).
    shared = branches.git(a["path"], "log", "--format=%s", base, f"^{MAIN}", check=False).splitlines()
    owners = [x for x in (a, b) if any(s.endswith(f"(todo #{x['todo_id']})") for s in shared)]
    if len(owners) == 1:
        return owners[0]
    # Otherwise the shorter one is taken as the parent.
    return min((a, b), key=lambda x: (x["ahead"], x["todo_id"]))


def _parents(found: list[dict]) -> dict[int, set[int]]:
    parents: dict[int, set[int]] = {}
    for i, a in enumerate(found):
        for b in found[i + 1:]:
            if a["path"] != b["path"] or a["todo_id"] == b["todo_id"]:
                continue
            p = _parent(a, b)
            if p is not None:
                child = b if p is a else a
                parents.setdefault(child["todo_id"], set()).add(p["todo_id"])
    return parents


def _order(ids: Iterable[int], parents: dict[int, set[int]]) -> list[int]:
    """Parents first, then by todo number (a cycle, which git cannot really make, falls back to that order)."""
    left, order = set(ids), []
    while left:
        free = sorted(i for i in left if not (parents.get(i, set()) & left)) or sorted(left)
        order.append(free[0])
        left.discard(free[0])
    return order


EXHAUSTIVE_MAX = 8
GIT_IDENTITY = {"GIT_AUTHOR_NAME": "ultron-hub", "GIT_AUTHOR_EMAIL": "ultron-hub@localhost",
                "GIT_COMMITTER_NAME": "ultron-hub", "GIT_COMMITTER_EMAIL": "ultron-hub@localhost"}


def _git_rc(path: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                          timeout=branches.GIT_TIMEOUT_S, env={**branches._env(), **GIT_IDENTITY}, check=False)
    return proc.returncode, proc.stdout


def _merge_tree(path: Path, left: str, right: str) -> tuple[str | None, list[str]]:
    """(tree, []) when `right` merges cleanly into `left`, else (None, conflicted files). Read only."""
    rc, out = _git_rc(path, "merge-tree", "--write-tree", "--name-only", left, right)
    lines = out.splitlines()
    if rc == 0 and lines:
        return lines[0], []
    if rc == 1 and lines:
        files = []
        for line in lines[1:]:
            if not line:
                break
            files.append(line)
        return None, files
    return (lines[0] if lines else None), []  # git could not simulate: not a reason to leave a todo out


class _Simulation:
    """Merges the todos one after the other on main without touching anything; every prefix is remembered."""

    def __init__(self, by_todo: dict[int, list[dict]]):
        self.by_todo = by_todo
        self.memo: dict[tuple[int, ...], dict | None] = {(): {}}

    def _add(self, state: dict, tid: int) -> dict | None:
        new = dict(state)
        for b in self.by_todo[tid]:
            cur = new.get(b["path"]) or branches.git(b["path"], "rev-parse", MAIN)
            tree, files = _merge_tree(b["path"], cur, b["sha"])
            if files or tree is None:
                if files:
                    return None
                continue  # not simulable: keep the state
            _, commit = _git_rc(b["path"], "commit-tree", tree, "-p", cur, "-p", b["sha"], "-m", "simulation")
            new[b["path"]] = commit.strip() or cur
        return new

    def chain(self, ids: tuple[int, ...]) -> dict | None:
        if ids not in self.memo:
            before = self.chain(ids[:-1])
            self.memo[ids] = None if before is None else self._add(before, ids[-1])
        return self.memo[ids]


def _conflict_free(by_todo: dict[int, list[dict]], candidates: list[int], parents: dict[int, set[int]]) -> list[int]:
    """The largest set of candidates that merges in a chain with no conflict (see module doc), in merge order."""
    sim = _Simulation(by_todo)
    cand = set(candidates)

    def closed(sub: Iterable[int]) -> bool:
        s = set(sub)
        return all((parents.get(t, set()) & cand) <= s for t in s)

    def works(sub: Iterable[int]) -> bool:
        return closed(sub) and sim.chain(tuple(_order(sub, parents))) is not None

    ordered = sorted(candidates)
    if len(ordered) <= EXHAUSTIVE_MAX:
        for size in range(len(ordered), 0, -1):
            for sub in itertools.combinations(ordered, size):
                if works(sub):
                    return _order(sub, parents)
        return []
    pair = {t: 0 for t in ordered}
    for a, b in itertools.combinations(ordered, 2):
        if sim.chain((a,)) is not None and sim.chain((b,)) is not None and not works((a, b)) and closed((a, b)):
            pair[a] += 1
            pair[b] += 1
    kept: list[int] = []
    for t in sorted(ordered, key=lambda t: (pair[t], t)):
        need = {t}
        todo = [t]
        while todo:  # its stacked parents come with it
            for p in parents.get(todo.pop(), ()):
                if p in cand and p not in need:
                    need.add(p)
                    todo.append(p)
        trial = sorted(set(kept) | need)
        if works(trial):
            kept = trial
    return _order(kept, parents)


def _conflict_reason(by_todo: dict[int, list[dict]], tid: int, lot_ids: list[int]) -> str:
    for b in by_todo[tid]:  # alone on main first: the lot is not to blame if main itself conflicts
        tree, files = _merge_tree(b["path"], branches.git(b["path"], "rev-parse", MAIN), b["sha"])
        if tree is None and files:
            return f"conflit avec main ({files[0]}) : à rebaser sur main"
    for other in lot_ids:
        for b in by_todo[tid]:
            for o in by_todo[other]:
                if o["path"] != b["path"]:
                    continue
                tree, files = _merge_tree(b["path"], o["sha"], b["sha"])
                if tree is None and files:
                    return f"conflit avec #{other} ({files[0]}) : à rebaser après la fusion"
    return "conflit avec le reste du lot : à rebaser après la fusion"


def _action_line(todo_id: int, a: dict) -> str:
    # JSON (ASCII) for every free field: a declared string cannot hold a newline or forge another line.
    return (f"{int(todo_id)}:action:{int(a['n'])}:{json.dumps(a['action'])}:{json.dumps(a['args'])}:"
            f"{json.dumps(a['command'])}:{a['status']}")


def empreinte(lot: list[dict]) -> str | None:
    rows = sorted((t["todo_id"], r["repo"], r["branch"], r["sha"]) for t in lot for r in t["repos"])
    lines = [":".join(str(x) for x in row) for row in rows]
    actions = sorted(((t["todo_id"], a["n"]), _action_line(t["todo_id"], a)) for t in lot for a in t.get("actions") or [])
    lines += [line for _, line in actions]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16] if lines else None


GATEWAY_RESTART = ("user_unit_restart", ["hermes-gateway"])
LOT_RESTART_OUTPUT = "fait par le redémarrage différé unique de la fin du lot (🔀 Tout fusionner)"


def _gateway_restart(a: dict) -> bool:
    return (a["action"], a["args"]) == GATEWAY_RESTART


def _declared(settings: Settings, todo_id: int, bs: list[dict]) -> tuple[list[dict], str | None]:
    """The after-merge actions declared at the head of the todo's ultron-hub branch (validated), or the error."""
    b = next((b for b in bs if b["path"] == settings.root), None)
    if b is None:
        return [], None  # actions are only read from ultron-hub (as for one 🔀)
    raw = branches.show(b["path"], b["sha"], after_merge.declaration_path(todo_id))
    if raw is None:
        return [], None
    try:
        parsed = after_merge.parse(raw)
    except InvalidRequestError as exc:
        return [], str(exc)
    keep = ("n", "action", "args", "reason", "command", "status", "error")
    actions = [{k: a[k] for k in keep} for a in parsed]
    for a in actions:
        a["plan"] = ("jamais" if a["status"] != "pending" else
                     "redémarrage du lot" if _gateway_restart(a) else "lancée")
    return actions, None


def _files(path: Path, sha: str) -> int:
    """Files the branch changes since it left main (`main...sha`): what the card announces (todo #401)."""
    out = branches.git(path, "diff", "--name-only", f"{MAIN}...{sha}", check=False)
    return len([line for line in out.splitlines() if line.strip()])


def _dirty(path: Path, branch: str) -> int:
    wt = branches.branch_worktree(path, branch)
    return len(branches.git(wt, "status", "--porcelain", check=False).splitlines()) if wt else 0


def lot(settings: Settings, sans: Iterable[int] = ()) -> dict:
    """The ready todos in merge order, the ones left out and why, and the lot's fingerprint (read only)."""
    sans = set(sans)
    found = _all_branches(settings)
    with TodoStore(settings.todo_db) as todos:
        open_tasks = {t.id: t.title for t in todos.list(status="todo", limit=100000)}
    by_todo: dict[int, list[dict]] = {}
    for b in found:
        by_todo.setdefault(b["todo_id"], []).append(b)
    parents = _parents(found)
    ready: dict[int, str | None] = {}  # todo -> None (in the lot) or why it is left out
    for tid, bs in sorted(by_todo.items()):
        if tid not in open_tasks or not any(b["ahead"] for b in bs):
            continue  # closed, or nothing to merge
        if not branches.tagged_own_commits(bs):
            continue  # no clean commit « (todo #<id>) » (stacked without its own work, agent at work): « en cours
        repos_seen = [b["repo"] for b in bs]
        multiple = sorted({r for r in repos_seen if repos_seen.count(r) > 1})
        dirty = sum(_dirty(b["path"], b["branch"]) for b in bs)
        # todo #401: the real reason first, « retirée » only for a todo that was ready: `check` removes every todo
        # the signature does not name, and the report must still tell why one was not even in the lot.
        ready[tid] = (f"plusieurs branches todo-{tid}/… dans {', '.join(multiple)}" if multiple else
                      f"worktree non propre ({dirty} fichier(s) non commité(s) ou non suivi(s))" if dirty else
                      "retirée du lot" if tid in sans else None)

    def propagate() -> None:
        changed = True
        while changed:  # a branch stacked on a todo out of the lot would bring that todo's code: left out too
            changed = False
            for tid, why in ready.items():
                if why is None:
                    out = sorted(p for p in parents.get(tid, ()) if p not in ready or ready[p] is not None)
                    if out:
                        ready[tid] = f"empilée sur #{out[0]}, qui n'est pas dans le lot"
                        changed = True

    propagate()
    candidates = [t for t, why in ready.items() if why is None]
    best = _conflict_free(by_todo, candidates, parents) if candidates else []
    for tid in candidates:
        if tid not in best and (parents.get(tid, set()) & set(candidates)) <= set(best):
            ready[tid] = _conflict_reason(by_todo, tid, best)
    propagate()
    in_lot = [t for t, why in ready.items() if why is None]
    items = []
    for tid in _order(in_lot, parents):
        bs = sorted(by_todo[tid], key=lambda b: b["repo"])
        actions, actions_error = _declared(settings, tid, bs)
        rows = [{"repo": b["repo"], "branch": b["branch"], "sha": b["sha"], "ahead": b["ahead"],
                 "files": _files(b["path"], b["sha"]), "label": branches.label(settings, b["repo"], b["branch"])}
                for b in bs]
        items.append({"todo_id": tid, "title": open_tasks[tid], "ahead": sum(b["ahead"] for b in bs),
                      "files": sum(r["files"] for r in rows),
                      "apres": sorted(parents.get(tid, set()) & set(in_lot)),
                      "repos": rows,
                      "actions": actions, "actions_error": actions_error})
    ecartees = [{"todo_id": t, "title": open_tasks[t], "raison": why} for t, why in sorted(ready.items()) if why]
    return {"lot": items, "ids": [t["todo_id"] for t in items], "ecartees": ecartees, "empreinte": empreinte(items)}


def _refuse(settings: Settings, todo_id: int, step: str, reason: str) -> None:
    branches.log_refusal(settings, todo_id, step, reason)
    raise InvalidRequestError(f"❌ Fusion du lot refusée : {reason}\n\nRien n'a été fusionné, main n'a pas changé. "
                              "À faire : rouvre « 🔀 Tout fusionner » pour voir le lot à jour.")


def check(settings: Settings, ids: list[int], expected: str) -> dict:
    """The lot of exactly `ids`, as it is now; refused if a todo is no longer ready or a branch moved."""
    candidates = {b["todo_id"] for b in _all_branches(settings)}
    data = lot(settings, sans=candidates - set(ids))
    for tid in ids:
        if tid not in data["ids"]:
            why = next((e["raison"] for e in data["ecartees"] if e["todo_id"] == tid),
                       "plus rien à fusionner (branche absente, déjà fusionnée ou todo close)")
            _refuse(settings, tid, "lot-not-ready", f"#{tid} n'est plus prête : {why}")
    if data["empreinte"] != expected:
        _refuse(settings, ids[0], "lot-moved",
                "une branche a bougé ou une action après fusion a changé depuis l'affichage de la carte")
    return data


def _moved(settings: Settings, item: dict) -> str | None:
    paths = repos.list_repos(settings)
    for r in item["repos"]:
        path = paths.get(r["repo"])
        if path is None or branches.git(path, "rev-parse", "--verify", "-q", r["branch"], check=False) != r["sha"]:
            return r["label"]
    return None


def _tool_digest() -> str | None:
    """SHA-256 of ultron-user as on disk (the file each action really runs), None if unreadable."""
    try:
        return hashlib.sha256(user_actions.repo_tool().read_bytes()).hexdigest()
    except OSError:
        return None


def _key(a: dict) -> tuple:
    return a["n"], a["action"], a["args"]


def _lot_by(by: dict, emp: str, n: int) -> dict:
    """Trace of an action approved by the lot's ✅: who, which lot, and a proof unique to the action."""
    out = {**by, "via": "fusion-lot", "empreinte": emp}
    if by.get("proof"):
        out["proof"] = f"{by['proof']}:{int(n)}"
    return out


def _launch_actions(settings: Settings, data: dict, results: list[dict], by: dict, launch: Callable,
                    tool_before: str | None) -> tuple[list[tuple[int, dict]], str | None]:
    """Launches the actions covered by the lot's signature (see module doc). Returns the gateway restarts to mark
    done once the lot's restart is planned, and a note when nothing could be launched."""
    if tool_before is None or _tool_digest() != tool_before:
        log.warning("ultron-user changed during the lot: after-merge actions left pending")
        return [], "ultron-user a changé pendant le lot : actions après fusion à valider une par une (🧰)"
    shown = {t["todo_id"]: t.get("actions") or [] for t in data["lot"]}
    gateway: list[tuple[int, dict]] = []
    for r in results:
        tid, card = r["todo_id"], shown.get(r["todo_id"]) or []
        recorded = r.get("after_merge") or []
        if not card and not recorded:
            continue
        if r.get("after_merge_error") or [_key(a) for a in recorded] != [_key(a) for a in card]:
            log.warning("todo=%d after-merge declaration on main differs from the card: left pending", tid)
            r["actions_note"] = "actions sur main différentes de la carte : à valider une par une (🧰)"
            continue
        for a, c in zip(recorded, card):
            if a["status"] != "pending" or c["status"] != "pending" or not a["command"] or a["command"] != c["command"]:
                continue  # invalid, already decided, or not what the card showed: never launched by the lot
            approval = _lot_by(by, data["empreinte"], a["n"])
            if _gateway_restart(a):
                gateway.append((tid, {**a, "by": approval}))
                continue
            try:
                after_merge.decide(settings, tid, a["n"], approve=True, by=approval, launch=launch)
            except Exception:
                log.exception("todo=%d after-merge #%d not launched by the lot", tid, a["n"])
    return gateway, None


def _mark_gateway(settings: Settings, gateway: list[tuple[int, dict]]) -> None:
    for tid, a in gateway:
        try:
            after_merge.decide(settings, tid, a["n"], approve=True, by=a["by"],
                               run_user=lambda action, args: {"status": "ok", "output": LOT_RESTART_OUTPUT})
        except Exception:
            log.exception("todo=%d after-merge #%d (gateway restart) not marked", tid, a["n"])


def _stop_reason(stopped: dict) -> str:
    """One short line: why the lot stopped on this todo (the full text follows in `lot_text`)."""
    if "result" in stopped:
        check = stopped["result"]["check"]
        return ("tests trop longs" if check.get("timeout") else
                "conflit avec main" if check.get("stage") == "conflict" else "tests en échec")
    first = (stopped.get("refusal") or "refus").strip().splitlines()[0]
    for prefix in ("❌ Fusion du lot refusée : ", "❌ Fusion refusée : ", "❌ "):
        first = first.removeprefix(prefix)
    return first[:160]


def _bilan(data: dict, done: set[int], stopped: dict | None, failed: dict[int, dict]) -> list[dict]:
    """Every todo of the signed lot, in its order: merged, or open and why (todo #401: nothing left unexplained)."""
    stop_id = stopped["todo_id"] if stopped else None
    out = []
    for item in data["lot"]:
        tid = item["todo_id"]
        waiting = [p for p in item["apres"] if p not in done]
        why = (None if tid in done else
               _stop_reason(stopped) if tid == stop_id else
               ("tests trop longs" if failed[tid]["check"].get("timeout") else "tests en échec") if tid in failed else
               f"attend #{waiting[0]}, pas fusionnée" if waiting else
               f"pas tentée : le lot s'est arrêté à #{stop_id}" if stop_id is not None else "pas tentée")
        out.append({"todo_id": tid, "title": item.get("title") or "", "fusionnee": tid in done, "raison": why})
    return out


def bilan_line(r: dict) -> str:
    """The lot's outcome in one line (journal, log): merged, left open and why, left out of the lot."""
    merged = [b for b in r["bilan"] if b["fusionnee"]]
    opened = [b for b in r["bilan"] if not b["fusionnee"]]
    parts = [f"Tout fusionner : {len(merged)}/{len(r['bilan'])} fusionnée(s)"
             + (" (" + ", ".join(f"#{b['todo_id']}" for b in merged) + ")" if merged else "")]
    if opened:
        parts.append("restée(s) ouverte(s) : " + ", ".join(f"#{b['todo_id']} ({b['raison']})" for b in opened))
    out = [e for e in r.get("ecartees") or [] if e["raison"] != "retirée du lot"]
    if out:
        parts.append("hors du lot : " + ", ".join(f"#{e['todo_id']} ({e['raison']})" for e in out[:10]))
    return " ; ".join(parts)


def _journal_bilan(settings: Settings, r: dict) -> None:
    """Todo #401: the lot leaves its own trace, also when it stopped (the dashboard journals only a success)."""
    line = bilan_line(r)
    log.warning("lot %s: %s", r["empreinte"], line)
    try:
        journal.record(settings, "ultron-hub", "fusion", line)
    except Exception:  # the merges are done: the trace is best effort
        log.exception("lot report not journalled")


def run(settings: Settings, data: dict, by: dict | None, restart: branches.Restarter | None = None,
        launch: Callable | None = None, restart_dash: branches.Restarter | None = None) -> dict:
    """Merge the lot in order; a todo refused by its tests is tried again after the others while a pass merges something
    (todo #378), any other refusal stops the lot; when every merge succeeded and `by` and `launch` are given
    (`launch` = `after_merge.launch_unit` from the CLI), launch the after-merge actions covered by the signature;
    then restart the gateway once if needed."""
    restart = restart or branches.restart_gateway
    tool_before = _tool_digest()
    progress = Progress(settings, data["ids"])
    results: list[dict] = []
    stopped: dict | None = None
    wanted: list[int] = []
    dash: list[int] = []
    pending, failed = list(data["lot"]), {}
    while pending and stopped is None:  # todo #378: a todo refused by its tests is tried again after the others
        retry, merged_now = [], False
        for item in pending:
            tid = item["todo_id"]
            if set(item["apres"]) & {i["todo_id"] for i in retry}:
                retry.append(item)  # its parent is not merged yet
                continue
            moved = _moved(settings, item)
            if moved:
                reason = f"la branche {moved} a bougé depuis l'affichage de la carte"
                branches.log_refusal(settings, tid, "lot-moved", reason)
                stopped = {"todo_id": tid, "refusal": branches.refusal_text(
                    tid, reason, "relis ce qui a changé, puis rouvre « 🔀 Tout fusionner ».")}
                break
            try:
                r = branches.merge_and_close(settings, tid, restart=lambda tid=tid: wanted.append(tid), by=by,
                                          progress=progress, restart_dash=lambda tid=tid: dash.append(tid))
            except InvalidRequestError as exc:  # already explained and journalled by branches
                stopped = {"todo_id": tid, "refusal": str(exc)}
                break
            except Exception as exc:
                log.exception("todo=%d lot merge failed", tid)
                stopped = {"todo_id": tid, "refusal": f"❌ Fusion de #{tid} impossible : {exc}"}
                break
            r["restart"] = None  # one restart for the whole lot, below
            if not r["closed"]:
                if r["check"]["stage"] != "tests":
                    stopped = {"todo_id": tid, "result": r}
                    break
                failed[tid] = r  # main may be red: another todo of the lot can repair it
                retry.append(item)
                continue
            failed.pop(tid, None)
            results.append(r)
            merged_now = True
        if stopped is None and retry and not merged_now:
            first = next(i["todo_id"] for i in retry if i["todo_id"] in failed)  # a child waits behind its parent
            stopped = {"todo_id": first, "result": failed[first]}
        pending = retry
    done = {r["todo_id"] for r in results}
    stop_id = stopped["todo_id"] if stopped else None
    remaining = [t for t in data["ids"] if t not in done and t != stop_id]
    out = {"ids": data["ids"], "empreinte": data["empreinte"], "results": results, "stopped": stopped,
           "remaining": remaining, "restart": None, "actions_note": None,
           "bilan": _bilan(data, done, stopped, failed), "ecartees": data.get("ecartees") or []}
    _journal_bilan(settings, out)
    gateway: list[tuple[int, dict]] = []
    if stopped is None and by and launch is not None:  # a refusal in the middle launches nothing
        progress.actions()
        gateway, out["actions_note"] = _launch_actions(settings, data, results, by, launch, tool_before)
    if wanted:
        progress.restart()
        try:
            restart()
            out["restart"] = "planned"
        except Exception as exc:  # merged and closed anyway: report, do not fail
            log.exception("gateway restart failed")
            out["restart"] = f"échec : {exc}"
    out["dashboard"] = None
    if dash:  # todo #294: once for the whole lot
        try:
            branches.restart_dashboard() if restart_dash is None else restart_dash()
            out["dashboard"] = "planned"
        except Exception as exc:
            log.exception("dashboard restart failed")
            out["dashboard"] = f"échec : {exc}"
    if out["restart"] == "planned":  # otherwise the declared restart stays pending, with its own button
        _mark_gateway(settings, gateway)
    for r in results:
        if r.get("after_merge"):
            r["after_merge"] = after_merge.load(settings, r["todo_id"])["actions"]
    return out

