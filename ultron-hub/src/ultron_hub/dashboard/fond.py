"""Long actions of the dashboard in the background (todo #267): 🔀 Fusionner, ✅ Tout fusionner, ✅ Exécuter after a merge.

A merge can restart the dashboard itself (after-merge action): run inside the HTTP request, the command died with the
service's cgroup and the page lost the answer. An entry of the catalogue with `fond=True` is therefore launched in its
OWN transient user unit (`systemd-run --user`, outside ultron-hub-dashboard.service), by this module's runner:

    python -P -m ultron_hub.dashboard.fond <job>

The dashboard writes `var/dashboard/fond/<job>.json` (action, values, device, an optional stdin such as a video's sentence; never argv nor the token), then starts the
unit with the signed token in its environment (120 s, single use). The runner rebuilds argv from the catalogue, runs the
CLI exactly as the dashboard would (`Actions._run`, then `Actions._finish`: same whitelist, journal and log), with
`ULTRON_HUB_SUIVI=<job>` so `merge_progress` tags its file, and writes the final answer into the job file.
`ultron-hub dashboard suivi <job>` (a `lecture` of the catalogue) turns both files into the steps the page draws.
"""

import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from ultron_hub import merge_progress
from ultron_hub.common.settings import Settings, load_settings

JOB_RE = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{8}")
LAUNCH_TIMEOUT_S = 30
GRACE_S = 120  # the unit's RuntimeMaxSec = the action's delay + this; a job still "en_cours" after that = interrupted
MERGES = ("merge", "fusion-lot")
# Steps of a merge, in order (labels shown by the page as they are).
VERIFICATION, TESTS, FUSION, ACTIONS, FIN = range(5)
LABELS = ("Vérification", "Tests", "Fusion", "Actions après fusion / redémarrage", "Terminé")


def directory(settings: Settings) -> Path:
    return settings.var_dir / "dashboard" / "fond"


def new_id(now: float) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-" + secrets.token_hex(4)


def write(settings: Settings, job: str, state: dict[str, Any]) -> None:
    """Atomic, 0600: the job's state (no secret: action, values, device, times, final answer)."""
    if not JOB_RE.fullmatch(job):
        raise ValueError("job id refused")
    folder = directory(settings)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = folder / f".{job}-{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    os.replace(tmp, folder / f"{job}.json")


def read(settings: Settings, job: str) -> dict[str, Any] | None:
    if not JOB_RE.fullmatch(job):
        return None
    try:
        state = json.loads((directory(settings) / f"{job}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def launch(settings: Settings, job: str, env: dict[str, str], delay_s: int) -> None:
    """Start the runner in its own transient unit and return at once (never `--wait`)."""
    import subprocess

    cmd = ["systemd-run", "--user", "--unit", f"ultron-dashboard-fond-{job}", "--collect", "--quiet",
           "-p", f"RuntimeMaxSec={int(delay_s) + GRACE_S}", f"--working-directory={settings.root}",
           *[f"--setenv={k}" for k in sorted(env)],  # NAME alone: the value comes from our env, never from argv (ps)
           sys.executable, "-P", "-m", "ultron_hub.dashboard.fond", job]
    run_env = {k: os.environ[k] for k in ("HOME", "PATH", "LANG", "XDG_RUNTIME_DIR") if k in os.environ}
    run_env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=LAUNCH_TIMEOUT_S, env={**run_env, **env}, check=False,
                          stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError(f"systemd-run a échoué : {proc.stderr.strip()[-300:]}")


def run_job(settings: Settings, job: str, acts: Any, token: str | None) -> dict[str, Any]:
    """The runner's work (also called in-process by the tests): run the job's command, record its final answer."""
    from ultron_hub.dashboard import actions, catalogue
    from ultron_hub.todo import closing

    state = read(settings, job)
    if state is None or state.get("etat") != "en_cours":
        raise SystemExit(f"job {job} unknown or already finished")
    action = catalogue.PAR_ID[state["action"]]
    args = {str(k): str(v) for k, v in (state.get("args") or {}).items()}
    stdin = state.get("stdin") if isinstance(state.get("stdin"), str) else None  # an optional field (todo #440)
    p = actions.Pending(str(state["device"]), "", action.id, args, 0.0, stdin)
    extra = {merge_progress.SUIVI_ENV: job, **({closing.TOKEN_ENV: token} if token else {})}
    try:
        result = acts._run(catalogue.commande(action, args, stdin), extra, stdin=stdin, timeout=action.delai)
        out = acts._finish(p, action, result)
    except Exception as exc:  # the job file must always end
        out = {"ok": False, "resume": "", "erreur": f"échec du lancement ({type(exc).__name__})"}
    write(settings, job, {**state, "etat": "fini", "fini": time.time(), "resultat": out})
    return out


# -- what the page draws ---------------------------------------------------------------------------------------------

def _merge_steps(state: dict[str, Any], progress: dict[str, Any] | None) -> tuple[int, str]:
    """(index of the current step, its detail) from merge_progress's file, if it belongs to this job."""
    if not progress or progress.get("suivi") != state.get("job"):
        return VERIFICATION, ""
    ids = [int(t) for t in progress.get("ids") or []]
    done = progress.get("done") or {}
    if progress.get("restart") or progress.get("step") == "actions":
        return ACTIONS, ""
    todo, step = progress.get("todo"), progress.get("step")
    if todo is None:
        return (ACTIONS, "") if ids and len(done) >= len(ids) else (VERIFICATION, "")
    rank = f"#{todo} ({ids.index(todo) + 1}/{len(ids)})" if todo in ids else f"#{todo}"
    if step == "essai":
        return (VERIFICATION if not done else TESTS), rank
    if step == "tests":
        return TESTS, rank
    return FUSION, rank  # fusion, cloture


def suivi(settings: Settings, job: str, now: float | None = None) -> dict[str, Any] | None:
    """The page's view of a job: `etapes` [{nom, etat (fait|en_cours|a_venir|refus), detail}], `fini`, `resultat`."""
    state = read(settings, job)
    if state is None:
        return None
    state["job"] = job
    now = time.time() if now is None else now
    merge = state.get("action") in MERGES
    try:
        progress = json.loads(merge_progress.path(settings).read_text(encoding="utf-8")) if merge else None
    except (OSError, ValueError):
        progress = None
    finished = state.get("etat") == "fini"
    result = state.get("resultat") if finished else None
    if not finished and now > float(state.get("lance") or 0) + int(state.get("delai") or 0) + GRACE_S:
        finished, result = True, {"ok": False, "resume": "", "erreur": "interrompue : aucune réponse dans le délai"}
    labels = LABELS if merge else ("En cours", "Terminé")
    current, detail = _merge_steps(state, progress) if merge else (0, "")
    ok = bool(result and result.get("ok"))
    if finished and ok:
        current, detail = len(labels), ""
    steps = []
    for i, nom in enumerate(labels):
        if i == len(labels) - 1 and finished and not ok:
            steps.append({"nom": "Refusé", "etat": "refus", "detail": ""})  # the reason: `resultat.erreur`, shown once
            continue
        etat = "fait" if i < current else "a_venir"
        if i == current and not finished:
            etat = "en_cours"
        elif i == current and finished and not ok:
            etat = "refus"
        steps.append({"nom": nom, "etat": etat, "detail": detail if i == current else ""})
    return {"suivi": job, "action": state.get("action"), "fini": finished, "etapes": steps, "resultat": result,
            "depuis_s": int(max(0, now - float(state.get("lance") or now)))}


def main(argv: list[str]) -> int:
    from ultron_hub.common.logging import get_logger, setup_logging
    from ultron_hub.dashboard import actions
    from ultron_hub.todo import closing

    if len(argv) != 1 or not JOB_RE.fullmatch(argv[0]):
        print("usage: python -m ultron_hub.dashboard.fond <job>", file=sys.stderr)
        return 2
    settings = load_settings()
    setup_logging(settings.logs_dir, settings.log_level, settings.log_file, settings.log_max_bytes,
                  settings.log_backup_count)
    token = os.environ.pop(closing.TOKEN_ENV, None)
    out = run_job(settings, argv[0], actions.Actions(), token)
    get_logger("dashboard").info("dashboard background job=%s ok=%s", argv[0], out.get("ok"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
