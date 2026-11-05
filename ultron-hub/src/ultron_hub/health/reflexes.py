"""Reflexes of the watcher (todo #161): known breakdowns, diagnosed by script (no AI), never repaired alone.

A reflex of config/sante.yaml = an error pattern → a plain-language diagnosis → a proposed action. When the
pattern shows in the last backup (or check) error and the reflex's own check passes, `health alertes` adds the
diagnosis to its Telegram message and keeps it in var/health/reflexes.json; /sante then shows its button.

The button is signed (closing, action "reflexe", target "<reflex>:<fingerprint>") and runs one ultron-user action,
which checks every condition again by itself at the moment of the press. Nothing is automatic in v1.

First reflex: a stale restic lock (machine switched off during the backup) → 🔓 Retirer le verrou
(`ultron-user restic_unlock_stale`: `restic unlock`, never --remove-all).

Controls without pattern or button (CONTROLES) include `merge-check-orphelin` (todo #395): a var/merge-check/wt-*|rv-*
worktree older than 1 h (on disk or in `git worktree list`) is only reported; the cleanup is the merge's own sweep
(`branches.sweep_merge_check`, under the merge lock), so this module removes nothing.
"""

from __future__ import annotations

import fcntl
import os
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from ultron_hub import user_actions
from ultron_hub.health import seuils

log = logging.getLogger("ultron_hub.health")

SOURCES = ("sauvegarde", "dashboard")
# Checks the code knows (closed list): name -> (button label, ultron-user action).
VERIFICATIONS = {"verrou_restic": ("🔓 Retirer le verrou", "restic_unlock_stale")}
# Checks without a pattern and without a button: they alone decide (todo #294).
CONTROLES = ("dashboard_a_jour", "tmux_hors_service", "push_abonnement", "hook_commit", "merge_check_orphelin", "doc_hermes",
             "video_skill", "session_sans_claude")
DASHBOARD_UNIT = "ultron-hub-dashboard.service"
DASHBOARD_PATH = "src/ultron_hub/dashboard/"
NOM_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,23}")
CIBLE_RE = re.compile(r"([a-z0-9][a-z0-9-]{0,23}):([0-9a-f]{12})")
VERROU_MIN_H = 6
DEFAUTS: list[dict[str, Any]] = [{
    "nom": "verrou-restic",
    "motif": "repository is already locked",
    "source": "sauvegarde",
    "verification": "verrou_restic",
    "verrou_min_h": VERROU_MIN_H,
    "diagnostic": "Verrou de sauvegarde périmé (machine coupée pendant la sauvegarde)",
    "action": "🔓 Retirer le verrou (bouton dans /sante)",
}]
PREUVES = "preuves-reflexes.txt"
DASHBOARD_DEFAUT = {
    "nom": "dashboard-a-jour",
    "motif": "-",
    "source": "dashboard",
    "verification": "dashboard_a_jour",
    "verrou_min_h": VERROU_MIN_H,
    "diagnostic": "Le dashboard tourne avec un code plus ancien que main",
    "action": "redémarrage automatique à la prochaine fusion, ou `systemctl --user restart ultron-hub-dashboard.service`",
}
DEFAUTS.append(DASHBOARD_DEFAUT)
TMUX_DEFAUT = {  # todo #354
    "nom": "tmux-hors-service",
    "motif": "-",
    "source": "dashboard",
    "verification": "tmux_hors_service",
    "verrou_min_h": VERROU_MIN_H,
    "diagnostic": "Les sessions ultron vivent dans un service : une fusion les fermera",
    "action": "fermer et rouvrir les sessions ultron",
}
DEFAUTS.append(TMUX_DEFAUT)
PUSH_DEFAUT = {  # todo #340
    "nom": "push-abonnement",
    "motif": "-",
    "source": "dashboard",
    "verification": "push_abonnement",
    "verrou_min_h": VERROU_MIN_H,
    "diagnostic": "L'abonnement aux notifications d'un appareil date de plus de 14 jours",
    "action": "ouvrir l'app, Moi > Notifications",
}
DEFAUTS.append(PUSH_DEFAUT)
HOOK_DEFAUT = {  # todo #415
    "nom": "hook-commit",
    "motif": "-",
    "source": "dashboard",
    "verification": "hook_commit",
    "verrou_min_h": VERROU_MIN_H,
    "diagnostic": "Le hook qui ajoute « (todo #N) » aux commits n'est pas installé",
    "action": "pour chaque dépôt de ~/workspace/projects/active (todo #527) : git -C <dépôt> config core.hooksPath "
              "~/workspace/projects/active/ultron-hub/deploy/git-hooks",
}
DEFAUTS.append(HOOK_DEFAUT)
MERGE_CHECK_DEFAUT = {  # todo #395
    "nom": "merge-check-orphelin",
    "motif": "-",
    "source": "dashboard",
    "verification": "merge_check_orphelin",
    "verrou_min_h": VERROU_MIN_H,
    "diagnostic": "Un worktree jetable de fusion est resté orphelin (fusion tuée)",
    "action": "balayé par la prochaine fusion (🔀 d'une branche) : branches.sweep_merge_check, sous le verrou de fusion",
    "seul": True,  # todo #472
}
DEFAUTS.append(MERGE_CHECK_DEFAUT)
DOC_HERMES_DEFAUT = {  # todo #257
    "nom": "doc-hermes",
    "motif": "-",
    "source": "dashboard",
    "verification": "doc_hermes",
    "verrou_min_h": VERROU_MIN_H,
    "diagnostic": "Une doc dit que Hermes est en service alors que hermes-gateway est inactif",
    "action": "corriger la phrase citée (ETAT.md, ~/notes/serveur.md, ~/notes/prompt/_socle.md)",
}
DEFAUTS.append(DOC_HERMES_DEFAUT)
VIDEO_DEFAUT = {  # todo #476
    "nom": "video-skill",
    "motif": "-",
    "source": "dashboard",
    "verification": "video_skill",
    "verrou_min_h": VERROU_MIN_H,
    "diagnostic": "Le skill vidéo a échoué récemment (« video skill failed » dans le journal)",
    "action": "lire var/logs/ultron-hub.log (dernière ligne « video skill failed »), puis relancer la collecte",
}
DEFAUTS.append(VIDEO_DEFAUT)
VIDEO_FAIL_MARK = "video skill failed"
VIDEO_FAIL_MAX_H = 24
VIDEO_LOG_TAIL = 400_000  # bytes read from the end of the log
SESSION_SANS_CLAUDE_DEFAUT = {  # todo #537
    "nom": "session-sans-claude",
    "motif": "-",
    "source": "dashboard",
    "verification": "session_sans_claude",
    "verrou_min_h": VERROU_MIN_H,
    "diagnostic": "Une session ultron s'est terminée en 0:00 (Claude n'a pas démarré)",
    "action": "lire `ultron-hub journal tail` (ligne « fin … durée=0:00 »), vérifier `claude --version`, relancer `ultron <chantier>`",
}
DEFAUTS.append(SESSION_SANS_CLAUDE_DEFAUT)
SESSION_VIDE_MAX_H = 24
SESSION_VIDE_RE = re.compile(r"^fin \S+ durée=0:00\b")
MERGE_CHECK_MAX_H = 1
DOC_HERMES_UNIT = "hermes-gateway.service"
# Present-tense claims that Hermes runs / is the main interface (false while hermes-gateway is inactive).
DOC_HERMES_RE = re.compile(
    r"hermes(?:-gateway)?[^.\n]{0,25}\best en service|hermes-gateway(?:\.service)?`? est un service"
    r"|(?:telegram|hermes)\s*=\s*interface principale|hermes\s*=\s*\**guichet", re.IGNORECASE)
HOOK_DIR = "deploy/git-hooks"
PUSH_MAX_AGE_J = 14
PROC_DIR = Path("/proc")  # tests point it at an empty directory
SERVICE_CGROUP_RE = re.compile(r"ultron-(hub|bot)[^/\n]*\.service")

Inspect = Callable[[int], dict[str, Any]]


def state_path(root: Path) -> Path:
    return root / "var" / "health" / "reflexes.json"


def _valide(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    nom, motif = entry.get("nom"), entry.get("motif")
    if entry.get("verification") in CONTROLES and motif is None:
        motif = "-"  # a control has no error pattern
    diag, action = entry.get("diagnostic"), entry.get("action")
    if not (isinstance(nom, str) and NOM_RE.fullmatch(nom) and isinstance(motif, str) and motif.strip()
            and isinstance(diag, str) and diag.strip() and isinstance(action, str) and action.strip()):
        return None
    if entry.get("source", "sauvegarde") not in SOURCES:
        return None
    verif = entry.get("verification")
    if verif is not None and verif not in VERIFICATIONS and verif not in CONTROLES:
        return None
    hours = entry.get("verrou_min_h", VERROU_MIN_H)
    if isinstance(hours, bool) or not isinstance(hours, int) or hours < VERROU_MIN_H:
        hours = VERROU_MIN_H  # never under the floor ultron-user enforces anyway
    out = {"nom": nom, "motif": motif.strip(), "source": entry.get("source", "sauvegarde"), "verification": verif,
           "verrou_min_h": hours, "diagnostic": diag.strip(), "action": action.strip()}
    if entry.get("seul") is True:  # todo #472: réglé sans geste de personne (pas une panne pour le conseiller)
        out["seul"] = True
    return out


def charger(path: Path | None = None) -> list[dict[str, Any]]:
    """The reflex table; missing or unreadable file = the defaults, an empty list = no reflex."""
    try:
        data: Any = yaml.safe_load((path or seuils.config_path()).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return [dict(r) for r in DEFAUTS]
    if not isinstance(data, dict) or not isinstance(data.get("reflexes"), list):
        return [dict(r) for r in DEFAUTS]
    out = [r for r in (_valide(e) for e in data["reflexes"]) if r is not None]
    seen: set[str] = set()
    return [r for r in out if not (r["nom"] in seen or seen.add(r["nom"]))]


def inspecter_dashboard(root: Path, run: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Whether the dashboard service started before the last merge into main that touched its code (read-only,
    never raises). {"ok": True, "stale": bool, "reason": str}; ok False = could not tell, no diagnosis."""
    import subprocess
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False))
    try:
        shown = run(["systemctl", "--user", "show", DASHBOARD_UNIT, "-p", "ActiveState", "-p", "ActiveEnterTimestamp"])
        props = dict(line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line)
        if props.get("ActiveState") != "active" or not props.get("ActiveEnterTimestamp"):
            return {"ok": False, "stale": False, "reason": "service non actif"}  # a failed unit has its own alert
        stamp = props["ActiveEnterTimestamp"].split()
        started = datetime.strptime(" ".join(stamp[:3]), "%a %Y-%m-%d %H:%M:%S").timestamp()  # local time
        log_ = run(["git", "-C", str(root), "log", "-1", "--first-parent", "-m", "--format=%ct", "main", "--", DASHBOARD_PATH])
        last = int(log_.stdout.strip()) if log_.returncode == 0 and log_.stdout.strip() else None
    except Exception as exc:
        return {"ok": False, "stale": False, "reason": f"inspection impossible ({type(exc).__name__})"}
    if last is None or started >= last:
        return {"ok": True, "stale": False, "reason": "à jour"}
    fmt = lambda t: datetime.fromtimestamp(t).strftime("%d/%m %H:%M")  # noqa: E731
    return {"ok": True, "stale": True, "reason": f"démarré le {fmt(started)}, code modifié le {fmt(last)}"}


def inspecter_tmux(proc: Path | None = None) -> dict[str, Any]:
    """Whether a tmux server lives in the cgroup of an ultron service (read-only, never raises; `proc` is injectable).
    {"ok": True, "in_service": bool, "reason": str}; ok False = could not tell."""
    try:
        for d in (proc or PROC_DIR).iterdir():
            if not d.name.isdigit():
                continue
            try:
                comm = (d / "comm").read_text().strip()
                cmdline = (d / "cmdline").read_text().replace("\0", " ")
                if not (comm == "tmux: server" or comm == "tmux" and "new-session" not in cmdline and "attach" not in cmdline):
                    continue
                cgroup = (d / "cgroup").read_text()
            except OSError:
                continue  # process gone or unreadable
            found = SERVICE_CGROUP_RE.search(cgroup)
            if found:
                return {"ok": True, "in_service": True, "reason": f"serveur tmux {d.name} dans {found.group(0)}"}
    except Exception as exc:
        return {"ok": False, "in_service": False, "reason": f"inspection impossible ({type(exc).__name__})"}
    return {"ok": True, "in_service": False, "reason": "serveur tmux hors service ou absent"}


def inspecter_push(now: float | None = None, store: Path | None = None) -> dict[str, Any]:
    """Devices whose push subscription is older than PUSH_MAX_AGE_J days (read-only, never raises, no URL nor key read
    out). {"ok": True, "stale": bool, "reason": str}; no subscription at all = nothing to say."""
    import time
    try:
        if store is None:
            from ultron_hub.dashboard import push
            store = push.STORE_DIR / push.STORE_FILE
        data = json.loads(store.read_text(encoding="utf-8"))
        now = time.time() if now is None else now
        old = []
        for device, row in sorted(data.items()):
            since = row.get("depuis") if isinstance(row, dict) else None
            if isinstance(since, (int, float)) and not isinstance(since, bool) and now - since > PUSH_MAX_AGE_J * 86400:
                old.append(f"{device} : {int((now - since) // 86400)} j")
    except FileNotFoundError:
        return {"ok": True, "stale": False, "reason": "aucun abonnement"}
    except Exception as exc:
        return {"ok": False, "stale": False, "reason": f"inspection impossible ({type(exc).__name__})"}
    return {"ok": True, "stale": bool(old), "reason": ", ".join(old) or "abonnements récents"}


def textes(metrics: dict[str, Any], source: str) -> list[str]:
    """Error texts a reflex's pattern is looked for in (no network)."""
    backup = metrics.get("backup") or {}
    if source != "sauvegarde":
        return []
    out = []
    if backup.get("last_run_status") == "error" and backup.get("last_run_error"):
        out.append(str(backup["last_run_error"]))
    if backup.get("last_check_status") == "error" and backup.get("last_check_error"):
        out.append(str(backup["last_check_error"]))
    return out


def inspecter_verrous(min_h: int) -> dict[str, Any]:
    """ultron-user's own inspection (one code for the diagnosis and the action), read-only. Never raises."""
    try:
        return user_actions._spec().inspect_locks(min_h, [])
    except Exception as exc:  # network, restic missing, timeout: no diagnosis this time
        return {"ok": False, "reason": f"inspection impossible ({type(exc).__name__})", "fingerprint": None, "locks": []}


def inspecter_hook(root: Path, run: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Whether git runs the commit-msg hook of deploy/git-hooks in the ultron-hub repository (read-only, never raises).
    {"ok": True, "missing": bool, "reason": str}; ok False = could not tell, no diagnosis."""
    import subprocess
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False))
    try:
        res = run(["git", "-C", str(root), "config", "--get", "core.hooksPath"])
    except Exception as exc:
        return {"ok": False, "missing": False, "reason": f"inspection impossible ({type(exc).__name__})"}
    value = res.stdout.strip() if res.returncode == 0 else ""
    hook = Path(value).expanduser() / "commit-msg" if value else None
    if hook is None or not Path(value).expanduser().is_absolute():
        return {"ok": True, "missing": True, "reason": "core.hooksPath absent ou relatif"}
    if not value.rstrip("/").endswith(HOOK_DIR) or not hook.is_file() or not os.access(hook, os.X_OK):
        return {"ok": True, "missing": True, "reason": f"core.hooksPath = {value} (pas de commit-msg exécutable)"}
    return {"ok": True, "missing": False, "reason": "installé"}


def inspecter_hooks(root: Path, repos: dict[str, Path] | None = None,
                    run: Callable[..., Any] | None = None) -> dict[str, Any]:
    """inspecter_hook for every repository of repos.list_repos (todo #527: a supertool commit missed its tag).
    Read-only, never raises; missing = at least one repository without the hook, named in the reason."""
    if repos is None:
        try:
            from ultron_hub import repos as repos_mod
            from ultron_hub.common.settings import load_settings
            repos = repos_mod.list_repos(load_settings(root))
        except Exception as exc:
            return {"ok": False, "missing": False, "reason": f"inspection impossible ({type(exc).__name__})"}
    missing, unknown = [], []
    for name, path in sorted((repos or {root.name: root}).items()):
        res = inspecter_hook(path, run)
        if not res.get("ok"):
            unknown.append(name)
        elif res.get("missing"):
            missing.append(f"{name} ({res.get('reason')})")
    if missing:
        return {"ok": True, "missing": True, "reason": "sans le hook : " + ", ".join(missing)}
    if unknown:
        return {"ok": False, "missing": False, "reason": "inspection impossible : " + ", ".join(unknown)}
    return {"ok": True, "missing": False, "reason": "installé"}


def inspecter_video(root: Path, now: float | None = None) -> dict[str, Any]:
    """Whether the hub log holds a "video skill failed" line from the last day (read-only, never raises).
    {"ok": True, "stale": bool, "reason": str}; ok False = could not tell, no diagnosis."""
    import time
    from datetime import datetime
    limit = (now if now is not None else time.time()) - VIDEO_FAIL_MAX_H * 3600
    last = None
    try:
        with (root / "var" / "logs" / "ultron-hub.log").open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - VIDEO_LOG_TAIL))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return {"ok": True, "stale": False, "reason": "pas de journal"}
    for line in lines:
        if VIDEO_FAIL_MARK in line:
            try:
                if datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").timestamp() >= limit:
                    last = line[:200]
            except ValueError:
                continue
    return {"ok": True, "stale": last is not None, "reason": last or "aucun échec récent"}


def inspecter_session_sans_claude(settings: Any = None, now: float | None = None,
                                  rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Whether the common journal holds a "fin <session> durée=0:00" line (source ultron, type session) from the last day:
    Claude never started in that session (read-only, never raises). {"ok": True, "stale": bool, "reason": str}."""
    import time
    from datetime import datetime
    try:
        if rows is None:
            from ultron_hub.common.settings import load_settings
            from ultron_hub.esprit import store
            rows = store._read_jsonl(store.root(settings or load_settings()) / store.JOURNAL)
    except Exception as exc:
        return {"ok": False, "stale": False, "reason": f"inspection impossible ({type(exc).__name__})"}
    limit = (now if now is not None else time.time()) - SESSION_VIDE_MAX_H * 3600
    last = None
    for row in rows:
        if row.get("source") != "ultron" or row.get("type") != "session":
            continue
        texte = str(row.get("texte") or "")
        if not SESSION_VIDE_RE.match(texte):
            continue
        try:
            if datetime.fromisoformat(str(row.get("date"))).timestamp() >= limit:
                last = f"{str(row.get('date'))[:16]} {texte}"[:200]
        except ValueError:
            continue
    return {"ok": True, "stale": last is not None, "reason": last or "aucune session sans Claude récente"}


def inspecter_doc_hermes(root: Path, run: Callable[..., Any] | None = None,
                         files: list[Path] | None = None) -> dict[str, Any]:
    """Docs claiming Hermes is in service while hermes-gateway is not active (read-only, never raises).
    {"ok": True, "stale": bool, "reason": str}; ok False = could not tell (unit state or files unreadable), no diagnosis."""
    import subprocess
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False))
    try:
        res = run(["systemctl", "--user", "is-active", DOC_HERMES_UNIT])
        etat = res.stdout.strip()
        if not etat:
            return {"ok": False, "stale": False, "reason": "état du service illisible"}
        if etat == "active":
            return {"ok": True, "stale": False, "reason": "hermes-gateway actif : la doc peut en parler au présent"}
        home = Path.home()
        hits = []
        for f in files if files is not None else [root / "ETAT.md", home / "notes" / "serveur.md",
                                                  home / "notes" / "prompt" / "_socle.md"]:
            if not f.is_file():
                continue
            for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if DOC_HERMES_RE.search(line):
                    hits.append(f"{f.name}:{n}")
    except Exception as exc:
        return {"ok": False, "stale": False, "reason": f"inspection impossible ({type(exc).__name__})"}
    return {"ok": True, "stale": bool(hits),
            "reason": (f"hermes-gateway {etat}; phrases en conflit : " + ", ".join(hits[:6])) if hits
            else f"hermes-gateway {etat}; aucune phrase en conflit"}


def inspecter_merge_check(root: Path, run: Callable[..., Any] | None = None, now: float | None = None) -> dict[str, Any]:
    """Throwaway merge worktrees (var/merge-check/wt-*, rv-*) older than MERGE_CHECK_MAX_H, on disk or registered in
    `git worktree list` (read-only, never raises, nothing removed: the sweep belongs to the merge, under its lock).
    {"ok": True, "stale": bool, "reason": str}; a registered entry whose directory is gone has no age: reported."""
    import subprocess
    import time
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False))
    try:
        base = (root / "var" / "merge-check").resolve()
        now = time.time() if now is None else now
        found: dict[str, float | None] = {}
        if base.is_dir():
            for work in [*base.glob("wt-*"), *base.glob("rv-*")]:
                found[work.name] = work.stat().st_mtime
        listed = run(["git", "-C", str(root), "worktree", "list", "--porcelain"])
        for line in (listed.stdout if listed.returncode == 0 else "").splitlines():
            if line.startswith("worktree "):
                path = Path(line.removeprefix("worktree "))
                if path.parent == base and re.fullmatch(r"(wt|rv)-.*", path.name):
                    found.setdefault(path.name, None)
        old = []
        for name, mtime in sorted(found.items()):
            if mtime is None:
                old.append(f"{name} (enregistré, dossier absent)")
            elif now - mtime > MERGE_CHECK_MAX_H * 3600:
                old.append(f"{name} ({(now - mtime) / 3600:.1f} h)")
    except Exception as exc:
        return {"ok": False, "stale": False, "reason": f"inspection impossible ({type(exc).__name__})"}
    return {"ok": True, "stale": bool(old), "reason": ", ".join(old) or "aucun worktree de fusion orphelin"}


def diagnostiquer(metrics: dict[str, Any], reflexes: list[dict[str, Any]] | None = None,
                  inspect: Inspect | None = None, inspect_dashboard: Callable[[], dict[str, Any]] | None = None,
                  inspect_tmux: Callable[[], dict[str, Any]] | None = None,
                  inspect_push: Callable[[], dict[str, Any]] | None = None,
                  inspect_hook: Callable[[], dict[str, Any]] | None = None,
                  inspect_merge_check: Callable[[], dict[str, Any]] | None = None,
                  inspect_doc: Callable[[], dict[str, Any]] | None = None,
                  inspect_video: Callable[[], dict[str, Any]] | None = None,
                  inspect_session: Callable[[], dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The reflexes whose pattern is found AND whose check passes, as diagnoses (with their button, if any)."""
    found = []
    for r in charger() if reflexes is None else reflexes:
        if r["verification"] == "dashboard_a_jour":
            res = (inspect_dashboard or (lambda: inspecter_dashboard(Path(__file__).resolve().parents[3])))()
            if res.get("ok") and res.get("stale"):
                found.append({"nom": r["nom"], "diagnostic": r["diagnostic"], "action": r["action"],
                              "detail": res.get("reason"), "bouton": None})
            continue
        if r["verification"] == "push_abonnement":
            res = (inspect_push or inspecter_push)()
            if res.get("ok") and res.get("stale"):
                found.append({"nom": r["nom"], "diagnostic": r["diagnostic"], "action": r["action"],
                              "detail": res.get("reason"), "bouton": None})
            continue
        if r["verification"] == "hook_commit":
            res = (inspect_hook or (lambda: inspecter_hooks(Path(__file__).resolve().parents[3])))()
            if res.get("ok") and res.get("missing"):
                found.append({"nom": r["nom"], "diagnostic": r["diagnostic"], "action": r["action"],
                              "detail": res.get("reason"), "bouton": None})
            continue
        if r["verification"] == "doc_hermes":
            res = (inspect_doc or (lambda: inspecter_doc_hermes(Path(__file__).resolve().parents[3])))()
            if res.get("ok") and res.get("stale"):
                found.append({"nom": r["nom"], "diagnostic": r["diagnostic"], "action": r["action"],
                              "detail": res.get("reason"), "bouton": None})
            continue
        if r["verification"] == "video_skill":
            res = (inspect_video or (lambda: inspecter_video(Path(__file__).resolve().parents[3])))()
            if res.get("ok") and res.get("stale"):
                found.append({"nom": r["nom"], "diagnostic": r["diagnostic"], "action": r["action"],
                              "detail": res.get("reason"), "bouton": None})
            continue
        if r["verification"] == "session_sans_claude":
            res = (inspect_session or inspecter_session_sans_claude)()
            if res.get("ok") and res.get("stale"):
                found.append({"nom": r["nom"], "diagnostic": r["diagnostic"], "action": r["action"],
                              "detail": res.get("reason"), "bouton": None})
            continue
        if r["verification"] == "merge_check_orphelin":
            res = (inspect_merge_check or (lambda: inspecter_merge_check(Path(__file__).resolve().parents[3])))()
            if res.get("ok") and res.get("stale"):
                found.append({"nom": r["nom"], "diagnostic": r["diagnostic"], "action": r["action"],
                              "detail": res.get("reason"), "bouton": None})
            continue
        if r["verification"] == "tmux_hors_service":
            res = (inspect_tmux or inspecter_tmux)()
            if res.get("ok") and res.get("in_service"):
                found.append({"nom": r["nom"], "diagnostic": r["diagnostic"], "action": r["action"],
                              "detail": res.get("reason"), "bouton": None})
            continue
        if not any(r["motif"].lower() in t.lower() for t in textes(metrics, r["source"])):
            continue
        diag: dict[str, Any] = {"nom": r["nom"], "diagnostic": r["diagnostic"], "action": r["action"],
                                "detail": None, "bouton": None}
        if r["verification"] == "verrou_restic":
            res = (inspect or inspecter_verrous)(r["verrou_min_h"])
            if not res.get("ok") or not res.get("fingerprint"):
                log.info("réflexe %s : motif trouvé, conditions non réunies (%s)", r["nom"], res.get("reason"))
                continue
            label, action = VERIFICATIONS["verrou_restic"]
            diag["detail"] = res.get("reason")
            diag["bouton"] = {"label": label, "cible": f"{r['nom']}:{res['fingerprint']}", "action": action,
                              "args": [str(r["verrou_min_h"]), res["fingerprint"]]}
        found.append(diag)
    return found


def anomalie(diag: dict[str, Any]) -> str:
    """One line of the alert message (health/alertes.py)."""
    detail = f" ({diag['detail']})" if diag.get("detail") else ""
    return f"🩺 diagnostic : {diag['diagnostic']}{detail} → action proposée : {diag['action']}"


def lire(root: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(state_path(root).read_text(encoding="utf-8"))
        diags = data["diagnostics"]
        return [d for d in diags if isinstance(d, dict) and isinstance(d.get("nom"), str)] if isinstance(diags, list) else []
    except (OSError, ValueError, KeyError, TypeError):
        return []


def ecrire(root: Path, diags: list[dict[str, Any]], now: datetime | None = None) -> None:
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    now = now or datetime.now(timezone.utc)
    tmp.write_text(json.dumps({"updated_at": now.isoformat(timespec="seconds"), "diagnostics": diags},
                              ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


def autoriser(root: Path, cible: str, *, human: bool) -> dict[str, Any]:
    """The user's proof (signed button or terminal) for this target, refused if already spent (replay)."""
    from ultron_hub.todo import closing
    folder = root / "var" / "health"
    folder.mkdir(parents=True, exist_ok=True)
    proofs = folder / PREUVES
    with (folder / ".preuves-reflexes.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # two presses with the same token cannot both pass

        def used(proof: str) -> bool:
            return proofs.is_file() and proof in proofs.read_text(encoding="utf-8").split()
        by = closing.authorize("reflexe", cible, human=human, what=f"Appliquer le réflexe {cible} ?", used=used)
        if by.get("proof"):
            with proofs.open("a", encoding="utf-8") as fh:
                fh.write(by["proof"] + "\n")
    return by


Runner = Callable[[str, list[str]], dict[str, Any]]


def agir(root: Path, cible: str, *, human: bool = False, run: Runner | None = None) -> dict[str, Any]:
    """The button of a diagnosis: proof, then the ultron-user action, which checks everything again itself.

    {"ok": bool, "message": str, "by": dict, "result": dict|None}. CloseRefused if the proof is missing or spent.
    """
    from ultron_hub.common.errors import InvalidRequestError
    if CIBLE_RE.fullmatch(cible or "") is None:
        raise InvalidRequestError(f"cible de réflexe invalide : {cible!r}")
    by = autoriser(root, cible, human=human)  # the token is spent even if the diagnosis is gone since
    diag = next((d for d in lire(root) if (d.get("bouton") or {}).get("cible") == cible), None)
    if diag is None:
        return {"ok": False, "by": by, "result": None,
                "message": "Ce diagnostic n'est plus d'actualité (la situation a changé depuis) : rien fait."}
    bouton = diag["bouton"]
    result = (run or user_actions.run)(bouton["action"], list(bouton["args"]))
    if result.get("status") != "ok":
        return {"ok": False, "by": by, "result": result,
                "message": f"Rien fait : {result.get('error') or 'erreur inconnue'}"}
    ecrire(root, [d for d in lire(root) if d.get("nom") != diag["nom"]])
    return {"ok": True, "by": by, "result": result, "message": f"✅ {bouton['label']} : fait."}
