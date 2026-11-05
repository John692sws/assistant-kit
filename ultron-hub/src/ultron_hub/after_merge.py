"""Server actions without sudo declared by a todo for after its 🔀 merge (todo #53).

The todo's branch adds `deploy/user/apres-fusion/todo-<id>.json` to ultron-hub:

    {"actions": [{"action": "hermes_menu_add", "args": ["profil"], "reason": "nouvelle commande /profil"}]}

The declaration is read from **main** (`git show`, so only the merged, reviewed content counts) at the end
of a successful 🔀, validated by ultron-user (exact preview) and kept in `var/after-merge/todo-<id>.json`.
Each action then waits for its own signed "✅ Exécuter" button (target "todo-<id>:<n>"). Nothing runs without it; a refused or invalid action never runs.
"""

import fcntl
import json
import os
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from ultron_hub import user_actions
from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.logging import get_logger
from ultron_hub.common.errors import UltronHubError
from ultron_hub.common.settings import Settings
log = get_logger("after-merge")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

DECLARATION_DIR = "deploy/user/apres-fusion"
MAX_ACTIONS = 5
MAX_REASON = 300
UNIT_MAX_S = 900  # RuntimeMaxSec of the transient unit (ultron-user stops itself at 600 s)
LAUNCH_TIMEOUT_S = UNIT_MAX_S + 60
GRACE_S = 30  # a "running" action younger than this may not have its unit yet
INTERRUPTED = ("coupée par un redémarrage ou un arrêt avant la fin : "
               "appuie sur 🧰 pour la relancer (elle repart de zéro)")
RETRYABLE = ("pending", "interrupted")


def declaration_path(todo_id: int) -> str:
    return f"{DECLARATION_DIR}/todo-{int(todo_id)}.json"


def target(todo_id: int, n: int) -> str:
    """Signed target of the "✅ Exécuter" button (closing.authorize("user", ...))."""
    return f"todo-{int(todo_id)}:{int(n)}"


def _dir(settings: Settings) -> Path:
    return settings.var_dir / "after-merge"


def _path(settings: Settings, todo_id: int) -> Path:
    return _dir(settings) / f"todo-{int(todo_id)}.json"


@contextmanager
def _lock(settings: Settings) -> Iterator[None]:
    _dir(settings).mkdir(parents=True, exist_ok=True)
    with open(_dir(settings) / ".lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def _save(settings: Settings, state: dict) -> None:
    path = _path(settings, state["todo_id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load(settings: Settings, todo_id: int) -> dict:
    path = _path(settings, todo_id)
    if not path.is_file():
        return {"todo_id": int(todo_id), "actions": [], "proofs": []}
    return json.loads(path.read_text(encoding="utf-8"))


def parse(raw: str) -> list[dict]:
    """The declared actions, each validated (exact preview) or marked invalid; never more than MAX_ACTIONS."""
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InvalidRequestError(f"déclaration après fusion illisible : {exc}") from None
    declared = data.get("actions") if isinstance(data, dict) else None
    if not isinstance(declared, list):
        raise InvalidRequestError("déclaration après fusion : {\"actions\": [...]} attendu")
    entries = []
    for req in declared[:MAX_ACTIONS]:
        req = req if isinstance(req, dict) else {}
        action, args = req.get("action"), req.get("args") or []
        entry = {"n": len(entries) + 1, "action": action if isinstance(action, str) else "",
                 "args": args if isinstance(args, list) and all(isinstance(a, str) for a in args) else [],
                 "reason": str(req.get("reason") or "")[:MAX_REASON], "command": None, "status": "pending",
                 "error": None, "output": "", "decided_at": None}
        try:
            entry["command"] = user_actions.preview(action, args)
        except InvalidRequestError as exc:
            entry.update(status="invalid", error=str(exc))
        entries.append(entry)
    return entries


def record(settings: Settings, todo_id: int, show: Callable[[str], str | None]) -> list[dict]:
    """After a successful merge: reads the declaration from main (`show(path)` = `git show main:<path>`, None if
    absent) and stores its actions, all pending. Recorded once: a second merge keeps the first decisions."""
    raw = show(declaration_path(todo_id))
    if raw is None:
        return []
    with _lock(settings):
        state = load(settings, todo_id)
        if state["actions"]:
            return state["actions"]
        state["actions"] = parse(raw)
        state["recorded_at"] = now()
        _save(settings, state)
    log.info("todo=%d %d after-merge action(s) recorded", todo_id, len(state["actions"]))
    return state["actions"]


def _find(state: dict, n: int) -> dict:
    for req in state["actions"]:
        if req["n"] == n:
            return req
    raise InvalidRequestError(f"action après fusion #{n} introuvable pour la tâche #{state['todo_id']}")


def proof_used(settings: Settings, todo_id: int, proof: str) -> bool:
    return proof in (load(settings, todo_id).get("proofs") or [])


def decide(settings: Settings, todo_id: int, n: int, approve: bool,
           run_user: Callable[[str, list], dict] = user_actions.run, by: dict | None = None, *,
           launch: Callable[[Settings, int, int], None] | None = None) -> dict:
    """Runs (signed `user` on "todo-<id>:<n>") or refuses one pending or interrupted action declared for after the
    merge. With `launch` (the CLI passes `launch_unit`) the action runs in its own unit; without it, inline."""
    if approve and not by:
        raise InvalidRequestError("exécution sans preuve : bouton « ✅ Exécuter » ou --i-am-human requis")
    reconcile(settings, todo_id)
    with _lock(settings):
        state = load(settings, todo_id)
        req = _find(state, n)
        if req["status"] not in RETRYABLE:
            raise InvalidRequestError(f"action après fusion #{n} déjà traitée ({req['status']})")
        if approve:
            proof = by.get("proof")
            if proof:
                if proof in state["proofs"]:
                    raise InvalidRequestError("jeton de clôture déjà utilisé")
                state["proofs"].append(proof)
            req["approval"] = {**by, "at": now()}
            req["error"], req["output"] = None, ""
        req["decided_at"] = now()
        req["status"] = "running" if approve else "refused"
        _save(settings, state)
    if not approve:
        return req
    log.warning("todo=%d after-merge #%d approved by %s: %s %s", todo_id, n, by.get("by"), req["action"], req["args"])
    if launch is None:
        return execute(settings, todo_id, n, run_user)
    try:
        launch(settings, todo_id, n)
    except Exception as exc:  # unit not started (or caller timed out)
        _finish(settings, todo_id, n, "failed", error=f"{type(exc).__name__}: {exc}")
    return _settle(settings, todo_id, n)


def _settle(settings: Settings, todo_id: int, n: int) -> dict:
    """After the unit returned: its recorded result, or "interrupted" if it died without recording one."""
    with _lock(settings):
        state = load(settings, todo_id)
        req = _find(state, n)
        if req["status"] == "running":
            req.update(status="interrupted", error=INTERRUPTED, finished_at=now())
            _save(settings, state)
        return req


def _finish(settings: Settings, todo_id: int, n: int, status: str, error: str | None = None, output: str = "") -> None:
    with _lock(settings):
        state = load(settings, todo_id)
        _find(state, n).update(status=status, error=error, output=output[-3000:], finished_at=now())
        _save(settings, state)


def execute(settings: Settings, todo_id: int, n: int, run_user: Callable[[str, list], dict] = user_actions.run) -> dict:
    """Runs an action already approved (status "running", set by `decide`) and records the result. This is what
    the transient unit executes (`branch after-exec`); it never approves anything by itself."""
    req = _find(load(settings, todo_id), n)
    if req["status"] != "running":
        raise InvalidRequestError(f"action après fusion #{n} pas en cours ({req['status']})")
    try:
        result = run_user(req["action"], req["args"])
    except Exception as exc:
        result = {"status": "error", "error": f"{type(exc).__name__}: {exc}", "output": ""}
    _finish(settings, todo_id, n, "done" if result.get("status") == "ok" else "failed", result.get("error"),
            result.get("output") or "")
    return _find(load(settings, todo_id), n)


def unit_name(todo_id: int, n: int) -> str:
    return f"ultron-after-merge-{int(todo_id)}-{int(n)}"


def _systemd_env() -> dict[str, str]:
    return {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
            "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"}


def unit_alive(unit: str) -> bool:
    """Whether the transient unit still exists (activating/active/deactivating)."""
    proc = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True,
                          timeout=30, env=_systemd_env(), check=False)
    return proc.stdout.strip() in ("active", "activating", "deactivating", "reloading")


def launch_unit(settings: Settings, todo_id: int, n: int) -> None:
    """Runs `ultron-hub branch after-exec` in its own transient user unit, outside hermes-gateway's cgroup: a
    restart of the gateway (every merge plans one) kills this caller, never the action (todo #79, npm ci of #77).
    Waits for the unit; if the caller is killed meanwhile, the unit still finishes and records the result."""
    cli = settings.root / ".venv" / "bin" / "ultron-hub"
    cmd = ["systemd-run", "--user", "--unit", unit_name(todo_id, n), "--collect", "--quiet", "--wait",
           "-p", f"RuntimeMaxSec={UNIT_MAX_S}", f"--working-directory={settings.root}",
           f"--setenv=ULTRON_HUB_ROOT={settings.root}", "--setenv=LANG=C.UTF-8",
           str(cli), "branch", "after-exec", str(int(todo_id)), str(int(n))]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=LAUNCH_TIMEOUT_S, env=_systemd_env(), check=False)
    if proc.returncode != 0:
        raise UltronHubError(f"systemd-run a échoué : {proc.stderr.strip()[-300:]}")


def reconcile(settings: Settings, todo_id: int, alive: Callable[[str], bool] = unit_alive) -> dict:
    """Marks "running" actions whose unit is gone as "interrupted" (replayable). Done under the lock, which the
    unit also takes to record its result: a unit that just finished has already saved, never a false orphan."""
    with _lock(settings):
        state = load(settings, todo_id)
        changed = False
        for req in state["actions"]:
            if req["status"] != "running" or time.time() - _epoch(req.get("decided_at")) < GRACE_S:
                continue
            if alive(unit_name(todo_id, req["n"])):
                continue
            req.update(status="interrupted", error=INTERRUPTED, finished_at=now())
            changed = True
            log.warning("todo=%d after-merge #%d interrupted (unit gone)", todo_id, req["n"])
        if changed:
            _save(settings, state)
    return state


def _epoch(stamp: str | None) -> float:
    try:
        return datetime.fromisoformat(stamp).timestamp() if stamp else 0.0
    except ValueError:
        return 0.0


def pending(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Actions the user can still run: pending, or interrupted by a restart (replay)."""
    return [a for a in actions if a["status"] in RETRYABLE]
