"""Automatic merge of a green branch by the orchestrator (todo #254), and its signed revert.

`ultron-hub branch auto-merge <id>`: the path of 🔀 (`branches.merge_and_close`: trial merge in var/merge-check, tests on
the merged result in the sandbox, merge --no-ff, worktree and branch removed, delayed restart), but the todo is NOT
closed: it stays open, marked delivered (`livree`, by `auto:orchestrateur`), so it shows in 📥 À toi for the user's ✅.
No signed token: that is the point. Refused (nothing merged, WARNING + journal like a refused 🔀):
- from a runner job or a squad (`ULTRON_HUB_JOB`, `ULTRON_ESCOUADE_DIR`): only the interactive orchestrator uses it;
- when the todo has todo-<id>/… branches in another repository than ultron-hub (SENSITIVE is checked there only);
- when the branch's diff against main touches a SENSITIVE path (signing, merge, deploy/, dashboard auth, executors,
  sandbox, test guards, any CLAUDE.md, this module);
- when the todo declares after-merge actions (deploy/user/apres-fusion/todo-<id>.json: they need the user's button);
- when merge_and_close refuses (worktree dirty, no single branch, …) or the tests fail.
Todo #482 then #495, the user's signed advance authorisation (`autorisation.py`, given at the dashboard for 1 to 14
days, `autonomous_week`): while it is valid, only SECURITY refuses (root helper, secrets and tokens, sending to a third
party, data deletion, any CLAUDE.md (todo #505), signing of ✅, dashboard auth, this module) and the other repositories (supertool, novapro, …) are
merged under the same rules, if they have a test command (settings.yaml `repos.tests`: untested = refused); no restart
for them (merge_and_close). Without a valid one (absent, expired, forged, other SECURITY list): the rules above.

`ultron-hub branch revert-last`: `git revert -m 1` of the LAST automatic merge (var/fusions-auto.jsonl), made in a
throwaway worktree then fast-forwarded into main: refused if the revert conflicts, if main is not clean, if that merge is
no longer in main or was already reverted. Signed like `todo done` (action `revert-fusion`, target "<id>-<sha12>").

`ultron-hub branch auto-merge-lot` (todo #533): only while the authorisation is valid (else refused, nothing merged), and
never from a job or a squad. The lot is the one of `branch lot` (`branch_lot.lot`, same stacked order); each todo goes
through `auto_merge` (stays open, delivered, by auto:orchestrateur), one after the other. A todo refused by its tests is
tried again after the others, in passes while a pass merges something (as `merge-lot`, todo #378); any other refusal
(SECURITY, after-merge actions, conflict, ...) is listed with its reason and the lot goes on, except for the todos stacked
on it. One delayed restart at the end if ultron-hub changed. Result: `bilan`, per todo merged + sha or refused + reason.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from ultron_hub import after_merge, autorisation, branch_lot, branches, repos
from ultron_hub.branches import MAIN, git
from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import Settings
from ultron_hub.esprit import store as journal
from ultron_hub.todo.store import TodoStore

log = get_logger("auto_merge")

ACTOR = "auto:orchestrateur"
JOB_ENVS = ("ULTRON_HUB_JOB", "ULTRON_ESCOUADE_DIR")  # executors.claude_cli.JOB_ENV, esprit.contexte.SQUAD_ENV
# The one list of paths an automatic merge never touches (ultron-hub only). A trailing "/" = everything below.
SENSITIVE = (
    "src/ultron_hub/todo/closing.py",          # who may close / merge / revert (signatures)
    "hermes_plugin/close_key.py",              # the plugin's signing side
    "src/ultron_hub/branches.py",              # merge: trial merge, tests, merge --no-ff, the guard hook
    "src/ultron_hub/branch_lot.py",            # merge: the 🔀 Tout fusionner batch
    "src/ultron_hub/branch_commands.py",       # merge: the CLI that checks the proof before merging
    "src/ultron_hub/after_merge.py",           # merge: actions run after a merge
    "deploy/",                                 # root actions (priv/), ultron-user, units, prompts, after-merge files
    "src/ultron_hub/dashboard/auth.py",        # dashboard: authorised devices, signed proofs
    "src/ultron_hub/dashboard/server.py",      # dashboard: whois of the caller
    "src/ultron_hub/executors/",               # the runner's environment (claude -p)
    "src/ultron_hub/repos.py",                 # sandbox: bwrap of the tests (sandbox_argv, run_tests)
    "src/ultron_hub/veille/sandbox.py",        # sandbox: veille scripts under bwrap
    "src/ultron_hub/escouade/hooks.py",        # hooks: Claude Code hooks of the squads (pre/post/stop)
    "tests/conftest.py",                       # test guards (what the tests may touch)
    ".claude/",                                # Claude Code settings / hooks of the repository, if ever added
    "src/ultron_hub/auto_merge.py",            # these rules themselves
    "src/ultron_hub/autorisation.py",          # the user's signed advance authorisation of these rules
)
# Todo #482/#495: while the user's authorisation is valid, the only paths an automatic merge never touches (every repository).
SECURITY = (
    "deploy/priv/",                            # root helper (ultron-priv, installed root-owned) and its install
    "src/ultron_hub/privileged.py",            # root: what ultron-hub asks ultron-priv (sudo)
    "src/ultron_hub/priv_commands.py",         # root: previews of the helper's actions
    "deploy/user/ultron-user",                 # the server-actions helper (run after a signed ✅)
    "src/ultron_hub/user_actions.py",          # its validation and launch
    "src/ultron_hub/todo/closing.py",          # signing of ✅ (who may close / merge / revert)
    "hermes_plugin/close_key.py",              # signing of ✅, the plugin's side (close.key)
    "src/ultron_hub/dashboard/auth.py",        # dashboard auth: authorised devices, signed proofs
    "src/ultron_hub/dashboard/server.py",      # dashboard auth: whois of the caller, Host, Origin, cookie
    "src/ultron_hub/dashboard/session.py",     # dashboard auth: signed session cookie and its key
    "src/ultron_hub/envoi.py",                 # sending to a third party: Telegram (bot token)
    "src/ultron_hub/bot/",                     # sending to a third party: the direct Telegram door
    "src/ultron_hub/dashboard/push.py",        # sending to a third party: Web Push (VAPID key)
    "src/ultron_hub/dashboard/pushcrypto.py",  # its signing keys
    "src/ultron_hub/backup/",                  # data deletion: restic forget --prune, restore
    "src/ultron_hub/menage_lot.py",            # data deletion: todos given up, chantiers archived in one ✅
    "src/ultron_hub/auto_merge.py",            # these rules themselves
    "src/ultron_hub/autorisation.py",          # the user's signed advance authorisation of these rules
)
# File names refused in any folder, in SENSITIVE and in SECURITY: project rules change every agent's behaviour.
RULE_NAMES = ("CLAUDE.md",)  # project rules: a rule enters only after the user's 🔀 (todo #505)
SECRET_NAMES = (".env", ".env.*", "*.key", "*.pem", "auth.json", "*credentials*", "*secret*", "*token*")  # any folder
PARIS = autorisation.PARIS
LOG_NAME = "fusions-auto.jsonl"
SHA12_LEN = 12


def _refuse(settings: Settings, todo_id: int, step: str, reason: str, hint: str, files: list[str] = ()) -> None:
    branches._refuse(settings, todo_id, step, reason, hint, [(f, "") for f in files])


def autonomous_week(settings: Settings, now: float | None = None) -> bool:
    """True while the user's signed authorisation is valid (todo #495); `now` = a Unix time (tests), default the clock."""
    return autorisation.valide(settings, now) is not None


def _covered(path: str, listed: tuple[str, ...]) -> bool:
    return any(path == s or path.startswith(s if s.endswith("/") else s + "/") for s in listed)


def sensitive(paths: list[str]) -> list[str]:
    """The paths of `paths` that SENSITIVE covers."""
    return [p for p in paths if _covered(p, SENSITIVE) or p.rsplit("/", 1)[-1] in RULE_NAMES]


def security(paths: list[str]) -> list[str]:
    """The paths of `paths` that SECURITY covers, or whose name looks like a secret (todo #482)."""
    return [p for p in paths if _covered(p, SECURITY) or p.rsplit("/", 1)[-1] in RULE_NAMES
            or any(fnmatch(p.rsplit("/", 1)[-1], n) for n in SECRET_NAMES)]


def changed_files(root: Path, rev: str) -> list[str]:
    """Files the branch changes since it left main (both names of a rename)."""
    out = git(root, "diff", "--name-only", "--no-renames", f"{MAIN}...{rev}")
    return [line for line in out.splitlines() if line.strip()]


def check_caller(env: dict | None = None) -> str | None:
    """The job variable that forbids an automatic merge here, or None."""
    env = os.environ if env is None else env
    return next((name for name in JOB_ENVS if env.get(name)), None)


HINT = "garde le 🔀 de l'utilisateur pour cette tâche."


def refusals(settings: Settings, todo_id: int, found: list[dict], now: float | None = None) -> list[dict]:
    """Why an automatic merge of these branches would be refused (read only, in the order the guard checks them).

    `found` = `branches.targets`; each target is read at `t["rev"]` when pinned, else at its branch. Each refusal is
    {"step", "reason", "hint", "files"}; an empty list = nothing here stops the automatic merge. `now`: see
    `autonomous_week` (todo #482: SECURITY in every repository instead of SENSITIVE in ultron-hub only).
    """
    week = autonomous_week(settings, now)
    others = [t for t in found if t["path"] != settings.root]
    if week:
        untested = [t for t in others if repos.test_command(settings, t["repo"], t["path"]) is None]
        if untested:
            return [{"step": "auto-untested", "hint": HINT,
                     "reason": "fusion automatique refusée : un dépôt de la tâche n'a pas de commande de test "
                               "(settings.yaml repos.tests)",
                     "files": [branches.label(settings, t["repo"], t["branch"] or t["branches"][0]) for t in untested]}]
    elif others:
        return [{"step": "auto-multi-repo", "hint": HINT,
                 "reason": "fusion automatique refusée : la tâche a des branches dans d'autres dépôts (fichiers sensibles "
                           "vérifiés pour ultron-hub seulement)",
                 "files": [branches.label(settings, t["repo"], t["branch"] or t["branches"][0]) for t in others]}]
    out: list[dict] = []
    for t in found:
        rev = t.get("rev") or t["branch"]
        declared = after_merge.declaration_path(todo_id)
        if branches.show(t["path"], rev, declared) is not None:
            out.append({"step": "auto-after-merge", "hint": HINT, "files": [declared],
                        "reason": "fusion automatique refusée : la tâche déclare des actions après fusion (bouton de l'utilisateur)"})
            continue
        changed = changed_files(t["path"], rev)
        hits = security(changed) if week else sensitive(changed)
        if hits:
            files = hits if t["path"] == settings.root else [f"{f} ({t['repo']})" for f in hits]
            out.append({"step": "auto-sensitive", "hint": HINT, "files": files,
                        "reason": "fusion automatique refusée : la branche touche un fichier sensible "
                                  + ("de sécurité (root, secrets, envoi, suppression, signature, auth du dashboard ou "
                                     "fusion automatique)" if week else
                                     "(signature, fusion, deploy, dashboard, sandbox ou garde-fous de test)")})
    return out


def _guard(settings: Settings, todo_id: int, now: float | None = None):
    def guard(found: list[dict]) -> None:
        for r in refusals(settings, todo_id, found, now)[:1]:
            _refuse(settings, todo_id, r["step"], r["reason"], r["hint"], r["files"])
    return guard


def auto_merge(settings: Settings, todo_id: int, restart: branches.Restarter | None = None,
               env: dict | None = None, now: float | None = None,
               restart_dash: branches.Restarter | None = None) -> dict:
    """Merge a green branch without closing its todo; InvalidRequestError (logged) on refusal, result otherwise.
    `now`: the clock of `autonomous_week` (tests); `restart_dash`: see `branches.merge_and_close`."""
    job = check_caller(env)
    if job:
        _refuse(settings, todo_id, "auto-mission", f"fusion automatique refusée depuis un job ou une escouade ({job})",
                "seul l'orchestrateur interactif fusionne seul ; sinon 🔀 de l'utilisateur.")
    by = {"by": ACTOR}
    r = branches.merge_and_close(settings, todo_id, restart=restart or branches.restart_gateway, by=by, close=False,
                                 guard=_guard(settings, todo_id, now), restart_dash=restart_dash)
    r["auto"] = True
    if r["merged"]:
        merged = [x for x in r["repos"] if x["merged"]]
        _append(settings, {"type": "fusion", "todo_id": todo_id, "at": time.time(), "by": ACTOR,
                           "repos": [{"repo": x["repo"], "branch": x["branch"], "sha": x["sha"]} for x in merged]})
        log.warning("todo=%d automatic merge %s", todo_id, ", ".join(x["sha"][:SHA12_LEN] for x in merged))
    return r


# -- the lot under the user's advance authorisation (todo #533) ------------------------------------------------------

def _lot_refuse(step: str, reason: str) -> None:
    log.warning("step=%s reason=%s", step, reason)
    raise InvalidRequestError(f"❌ Fusion automatique du lot refusée : {reason}\n\nRien n'a été fusionné, main n'a pas "
                              "changé. À faire : garde le 🔀 Tout fusionner de l'utilisateur.")


def _short(text: str) -> str:
    first = (text or "refus").strip().splitlines()[0]
    for prefix in ("❌ Fusion refusée : ", "❌ "):
        first = first.removeprefix(prefix)
    return first


def _check_reason(check: dict) -> str:
    return ("tests trop longs" if check.get("timeout") else
            "conflit avec main" if check.get("stage") == "conflict" else
            "tests en échec" if check.get("stage") == "tests" else _short(check.get("detail") or "refus"))


def auto_merge_lot(settings: Settings, restart: branches.Restarter | None = None, env: dict | None = None,
                   now: float | None = None, restart_dash: branches.Restarter | None = None) -> dict:
    """Merge every ready todo alone, under a valid authorisation only (see module doc). InvalidRequestError when the
    whole lot is refused (job, no authorisation); else {"ids", "bilan", "ecartees", "fusionnees", "refusees",
    "restart", "dashboard"}, `bilan` in lot order: {"todo_id", "titre", "statut": "fusionnee", "sha", "repos"} or
    {"todo_id", "titre", "statut": "refusee", "raison", "detail"}."""
    job = check_caller(env)
    if job:
        _lot_refuse("auto-lot-mission", f"fusion automatique refusée depuis un job ou une escouade ({job})")
    if not autonomous_week(settings, now):
        _lot_refuse("auto-lot-no-authorisation",
                    "pas d'autorisation d'avance valide (🤝 absente, expirée ou périmètre changé)")
    data = branch_lot.lot(settings)
    titles = {t["todo_id"]: t["title"] for t in data["lot"]}
    wanted: list[int] = []
    dash: list[int] = []
    done: dict[int, dict] = {}
    failed: dict[int, dict] = {}

    def no(tid: int, reason: str, detail: str | None = None) -> None:
        done[tid] = {"todo_id": tid, "titre": titles[tid], "statut": "refusee", "raison": reason,
                     "detail": detail or reason}

    pending = list(data["lot"])
    while pending:  # as merge-lot (todo #378): a todo refused by its tests is tried again after the others
        retry, merged_now = [], False
        for item in pending:
            tid = item["todo_id"]
            parent = next((p for p in item["apres"] if done.get(p, {}).get("statut") == "refusee"), None)
            if parent is not None:  # it would bring its refused parent's code
                no(tid, f"empilée sur #{parent}, refusée")
                continue
            if set(item["apres"]) & {i["todo_id"] for i in retry}:
                retry.append(item)  # its parent is not merged yet
                continue
            try:
                r = auto_merge(settings, tid, restart=lambda tid=tid: wanted.append(tid), env=env, now=now,
                               restart_dash=lambda tid=tid: dash.append(tid))
            except InvalidRequestError as exc:  # already logged and journalled (auto-sensitive, after-merge, ...)
                no(tid, _short(str(exc)), str(exc))
                continue
            except Exception as exc:
                log.exception("todo=%d automatic lot merge failed", tid)
                no(tid, f"erreur : {exc}")
                continue
            if not r["check"]["ok"]:
                if r["check"].get("stage") == "tests":
                    failed[tid] = r  # main may be red: another todo of the lot can repair it
                    retry.append(item)
                else:
                    no(tid, _check_reason(r["check"]), r["check"].get("detail"))
                continue
            failed.pop(tid, None)
            merged = [{"repo": x["repo"], "sha": x["sha"]} for x in r["repos"] if x.get("merged")]
            done[tid] = {"todo_id": tid, "titre": titles[tid], "statut": "fusionnee",
                         "sha": merged[0]["sha"] if merged else None, "repos": merged}
            merged_now = True
        if retry and not merged_now:
            for item in retry:  # parents come first: a child sees its parent refused
                tid = item["todo_id"]
                if tid in failed:
                    no(tid, _check_reason(failed[tid]["check"]), failed[tid]["check"].get("detail"))
                else:
                    parent = next((p for p in item["apres"] if p in done), item["apres"][0])
                    no(tid, f"empilée sur #{parent}, refusée")
            break
        pending = retry
    bilan = [done[t] for t in data["ids"] if t in done]
    out = {"ids": data["ids"], "bilan": bilan, "ecartees": data.get("ecartees") or [],
           "fusionnees": [b["todo_id"] for b in bilan if b["statut"] == "fusionnee"],
           "refusees": [b["todo_id"] for b in bilan if b["statut"] == "refusee"], "restart": None, "dashboard": None}
    if bilan:
        log.warning("automatic lot: merged %s, refused %s", out["fusionnees"], out["refusees"])
        journal.record(settings, "ultron-hub", "fusion", "Fusion automatique du lot : " + ", ".join(
            f"#{b['todo_id']} " + (f"fusionnée {(b['sha'] or '')[:SHA12_LEN]}" if b["statut"] == "fusionnee"
                                   else f"refusée ({b['raison']})") for b in bilan))
    if wanted:  # one delayed restart for the whole lot
        try:
            (restart or branches.restart_gateway)()
            out["restart"] = "planned"
        except Exception as exc:
            log.exception("gateway restart failed")
            out["restart"] = f"échec : {exc}"
    if dash:
        try:
            (restart_dash or branches.restart_dashboard)()
            out["dashboard"] = "planned"
        except Exception as exc:
            log.exception("dashboard restart failed")
            out["dashboard"] = f"échec : {exc}"
    return out


# -- the log of automatic merges and the revert ---------------------------------------------------------------------

def _log_path(settings: Settings) -> Path:
    return settings.var_dir / LOG_NAME


def _append(settings: Settings, entry: dict) -> None:
    path = _log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _entries(settings: Settings) -> list[dict]:
    path = _log_path(settings)
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def target(entry: dict) -> str:
    """Signed target of ↩️: "<todo id>-<sha12 of the first merge commit>"."""
    return f"{int(entry['todo_id'])}-{entry['repos'][0]['sha'][:SHA12_LEN]}"


def last(settings: Settings) -> dict:
    """The last automatic merge (read only): {"fusion", "cible", "annulee", "proofs"}; fusion None if there is none."""
    entries = _entries(settings)
    fusion = next((e for e in reversed(entries) if e.get("type") == "fusion" and e.get("repos")), None)
    reverts = [e for e in entries if e.get("type") == "revert"]
    if fusion is None:
        return {"fusion": None, "cible": None, "annulee": False, "proofs": [e.get("proof") for e in reverts]}
    cible = target(fusion)
    return {"fusion": fusion, "cible": cible, "annulee": any(e.get("cible") == cible for e in reverts),
            "proofs": [e.get("proof") for e in reverts if e.get("proof")]}


def _no(text: str) -> None:
    log.warning("step=revert-last reason=%s", text)
    raise InvalidRequestError(f"❌ Annulation refusée : {text}")


def check_revert(settings: Settings, cible: str | None = None) -> dict:
    """The last automatic merge, if it may be reverted (else InvalidRequestError, nothing changed)."""
    info = last(settings)
    if info["fusion"] is None:
        _no("aucune fusion automatique à annuler")
    if cible is not None and cible != info["cible"]:
        _no(f"la dernière fusion automatique n'est plus {cible} (c'est {info['cible']}) : rouvre la carte")
    if info["annulee"]:
        _no(f"la fusion automatique {info['cible']} est déjà annulée")
    known = repos.list_repos(settings)
    for x in info["fusion"]["repos"]:
        root = known.get(x["repo"])
        if root is None:
            _no(f"dépôt {x['repo']} introuvable")
        if git(root, "branch", "--show-current") != MAIN:
            _no(f"{x['repo']} n'est pas sur main")
        if git(root, "status", "--porcelain", "--untracked-files=no"):
            _no(f"{x['repo']} a des modifications non commitées")
        proc = branches.subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", x["sha"], MAIN],
                                       capture_output=True, text=True, timeout=branches.GIT_TIMEOUT_S,
                                       env=branches._env(), check=False)
        if proc.returncode != 0:
            _no(f"la fusion {x['sha'][:SHA12_LEN]} n'est plus dans main de {x['repo']}")
    return info


def _trial_revert(settings: Settings, root: Path, sha: str, todo_id: int) -> str:
    """The revert commit, made in a throwaway worktree on main (main itself untouched); refused on conflict."""
    base = settings.var_dir / "merge-check"
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="rv-", dir=base))
    try:
        git(root, "worktree", "add", "--detach", "--force", str(work), MAIN)
        proc = branches.subprocess.run(
            ["git", "-C", str(work), "revert", "-m", "1", "--no-commit", sha], capture_output=True, text=True,
            timeout=branches.GIT_TIMEOUT_S, env=branches._env(), check=False)
        if proc.returncode != 0:
            conflicts = git(work, "diff", "--name-only", "--diff-filter=U", check=False).splitlines()
            git(work, "revert", "--abort", check=False)
            _no(f"le revert de {sha[:SHA12_LEN]} ne passe pas sans conflit"
                + (" : " + ", ".join(conflicts[:branches.MAX_FILES_SHOWN]) if conflicts else ""))
        git(work, "commit", "-m", f"Annulation de la fusion automatique {sha[:SHA12_LEN]} (todo #{todo_id})")
        return git(work, "rev-parse", "HEAD")
    finally:
        git(root, "worktree", "remove", "--force", str(work), check=False)
        shutil.rmtree(work, ignore_errors=True)
        git(root, "worktree", "prune", check=False)


def revert_last(settings: Settings, by: dict, cible: str | None = None,
                restart: branches.Restarter | None = None) -> dict:
    """Revert the last automatic merge in every repository it touched (the caller checked the signed proof)."""
    with branches.lock(settings):
        info = check_revert(settings, cible)
        fusion = info["fusion"]
        todo_id = int(fusion["todo_id"])
        known = repos.list_repos(settings)
        made = [(x, known[x["repo"]], _trial_revert(settings, known[x["repo"]], x["sha"], todo_id))
                for x in fusion["repos"]]
        for _x, root, rev in made:  # main may only move forward onto the revert prepared above
            git(root, "merge", "--ff-only", rev)
        reverts = [{"repo": x["repo"], "sha": x["sha"], "revert": rev} for x, _root, rev in made]
        _append(settings, {"type": "revert", "cible": info["cible"], "todo_id": todo_id, "at": time.time(),
                           "by": by.get("by"), "proof": by.get("proof"), "repos": reverts})
        try:
            with TodoStore(settings.todo_db) as todos:
                todos.mark_merge_reverted(todo_id, {"by": by.get("by"), "proof": by.get("proof"), "cible": info["cible"],
                                                    "branches": [x["branch"] for x in fusion["repos"]]})
        except Exception:
            log.exception("todo=%d revert event", todo_id)
        shas = ", ".join(r["revert"][:SHA12_LEN] for r in reverts)
        log.warning("todo=%d automatic merge %s reverted by %s: %s", todo_id, info["cible"], by.get("by"), shas)
        journal.record(settings, "ultron-hub", "fusion",
                       f"Fusion automatique annulée : todo #{todo_id}, {info['cible']} → {shas}", todo_id)
    result = {"todo_id": todo_id, "cible": info["cible"], "repos": reverts, "restart": None, "dashboard": None}
    if any(root == settings.root for _x, root, _rev in made):
        if any(root == settings.root and branches.dashboard_changed(
                {"merged": True, "path": root, "before": f"{rev}^", "merge_sha": rev}) for _x, root, rev in made):
            try:  # todo #294: the revert changed the dashboard's code back
                branches.restart_dashboard()
                result["dashboard"] = "planned"
            except Exception as exc:
                log.exception("dashboard restart failed")
                result["dashboard"] = f"échec : {exc}"
        try:
            (restart or branches.restart_gateway)()
            result["restart"] = "planned"
        except Exception as exc:
            log.exception("gateway restart failed")
            result["restart"] = f"échec : {exc}"
    return result
