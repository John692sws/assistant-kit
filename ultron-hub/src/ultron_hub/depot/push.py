"""Push quotidien du dépôt vers le GitHub privé (todo #454). Sans shell, liste d'arguments, timeout.

`git push origin main --tags` (jamais de push forcé). Le dernier push réussi est écrit dans
var/depot/dernier-push.json ; le contrôle de santé (`probe`) lit ce fichier et `origin/main` en local, sans réseau.
"""

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("ultron_hub.depot")

TIMEOUT_S = 120
PUSH_CMD = ["git", "push", "origin", "main", "--tags"]
Runner = Callable[..., subprocess.CompletedProcess]


def state_path(var_dir: Path) -> Path:
    return var_dir / "depot" / "dernier-push.json"


def read_state(var_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads(state_path(var_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(var_dir: Path, now: datetime) -> None:
    path = state_path(var_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"at": now.isoformat(timespec="seconds")}) + "\n", encoding="utf-8")
    tmp.replace(path)


def push(repo: Path, var_dir: Path, *, run: Runner = subprocess.run, now: datetime | None = None) -> dict[str, Any]:
    """Returns {ok, at, error}. Never raises; a failure is logged as WARNING and leaves the last success untouched."""
    now = now or datetime.now(timezone.utc)
    try:
        proc = run(PUSH_CMD, cwd=str(repo), capture_output=True, text=True, timeout=TIMEOUT_S, shell=False)
    except (OSError, subprocess.SubprocessError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    else:
        if proc.returncode == 0:
            _write_state(var_dir, now)
            return {"ok": True, "at": now.isoformat(timespec="seconds"), "error": None}
        error = f"git push a échoué (code {proc.returncode}) : {(proc.stderr or proc.stdout or '').strip()[-300:]}"
    log.warning("depot push: %s", error)
    return {"ok": False, "at": None, "error": error}


def _age_h(iso: str | None, now: datetime) -> float | None:
    try:
        at = datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None
    if at is None:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (now - at).total_seconds() / 3600


def probe(repo: Path, var_dir: Path, *, run: Runner = subprocess.run, now: datetime | None = None) -> dict[str, Any]:
    """Local only (no network): age of the last successful push and commits of main missing from origin/main."""
    now = now or datetime.now(timezone.utc)
    behind = None
    try:
        proc = run(["git", "rev-list", "--count", "origin/main..main"], cwd=str(repo), capture_output=True,
                   text=True, timeout=30, shell=False)
        if proc.returncode == 0:
            behind = int(proc.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        behind = None
    last = read_state(var_dir).get("at")
    return {"last_push_at": last, "last_push_age_h": _age_h(last, now), "behind": behind}
