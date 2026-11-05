"""Root actions through ultron-priv (deploy/priv/ultron-priv, installed root-owned in /usr/local/sbin).

ultron-hub never runs sudo with anything else. Validation uses the repo copy of
ultron-priv (same code, so previews match what root will run); the installed
copy is the real security boundary and validates again.
"""

import hashlib
import importlib.machinery
import importlib.util
import json
import subprocess
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any

from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.settings import DEFAULT_ROOT

HELPER = Path("/usr/local/sbin/ultron-priv")
RUN_TIMEOUT_S = 960  # above ultron-priv's own 900 s
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


def repo_helper() -> Path:
    # The code's own repository (not ULTRON_HUB_ROOT): the helper is part of the code, not of the data.
    return DEFAULT_ROOT / "deploy" / "priv" / "ultron-priv"


@lru_cache(maxsize=1)
def _spec() -> ModuleType:
    path = repo_helper()
    loader = importlib.machinery.SourceFileLoader("ultron_priv", str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("ultron_priv", loader))
    loader.exec_module(module)
    return module


def actions() -> tuple[str, ...]:
    return _spec().ACTIONS


def preview(action: str, args: list[str]) -> list[str]:
    """Exactly what root will do, one line per command or step, or InvalidRequestError."""
    spec = _spec()
    if not isinstance(action, str) or not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise InvalidRequestError("action and args must be strings")
    try:
        return spec.describe(action, list(args))
    except spec.Refused as exc:
        raise InvalidRequestError(str(exc)) from exc


def run(action: str, args: list[str]) -> dict[str, Any]:
    preview(action, args)  # refuse early, before sudo
    try:
        proc = subprocess.run(["sudo", "-n", str(HELPER), action, *args], capture_output=True, text=True,
                              timeout=RUN_TIMEOUT_S, env=ENV, stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": f"délai dépassé ({RUN_TIMEOUT_S} s)", "output": ""}
    lines = proc.stdout.strip().splitlines()
    try:
        return json.loads(lines[-1])
    except (IndexError, ValueError):
        detail = proc.stderr.strip()[-300:]
        if "password" in detail:
            detail = "ultron-priv n'est pas autorisé sans mot de passe (lancer deploy/priv/install.sh)"
        return {"status": "error", "error": detail or f"sortie illisible (code {proc.returncode})", "output": ""}


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def status() -> dict[str, Any]:
    installed = _sha256(HELPER)
    sudo_ok = subprocess.run(["sudo", "-n", "-l", str(HELPER)], capture_output=True, text=True, env=ENV,
                             stdin=subprocess.DEVNULL, timeout=15, check=False).returncode == 0
    owner_root = HELPER.exists() and HELPER.stat().st_uid == 0 and not HELPER.stat().st_mode & 0o022
    return {
        "installed": installed is not None,
        "up_to_date": installed is not None and installed == _sha256(repo_helper()),
        "root_owned": owner_root,
        "sudo_ok": sudo_ok,
        "actions": list(actions()),
    }
