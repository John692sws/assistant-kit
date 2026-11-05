"""Server actions without sudo through ultron-user (deploy/user/ultron-user, run from the repository).

Same model as `privileged` (ultron-priv), for what the user `ultron` may do outside the repository: Telegram
menu, ultron-hub user units, pinned packages in Hermes' Python, a few Hermes settings, a stale restic lock. A todo
only proposes (deploy/user/apres-fusion/, see after_merge); ultron-hub validates with the tool (exact preview) and runs it only after the signed
"✅ Exécuter" button. The tool validates again by itself when it runs.
"""

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

PYTHON = "/usr/bin/python3"
RUN_TIMEOUT_S = 660  # above ultron-user's own 600 s
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}  # the tool builds its own environment anyway
STATUS = {"pending": "⏳ en attente", "running": "⚙️ en cours", "done": "✅ faite", "failed": "❌ échec",
          "refused": "✖ refusée", "invalid": "⛔ hors liste blanche", "interrupted": "⚠️ interrompue"}
MAX_TEXT = 4000


def action_line(req: dict[str, Any]) -> str:
    return f"#{req['n']} {req['action']} {' '.join(req['args'])}".strip()


def action_text(req: dict[str, Any], owner: str = "") -> str:
    """Screen of a server action without sudo (ultron-user): the exact commands, before "✅ Exécuter"."""
    lines = [f"🧰 Action serveur {action_line(req)} — {STATUS.get(req['status'], req['status'])}"]
    if owner:
        lines.append(owner)
    lines += ["", f"Pourquoi : {req['reason'] or '(non précisé)'}"]
    if req.get("command"):
        lines += ["", "Ce qui sera exécuté (utilisateur ultron, sans sudo), exactement :"]
        lines += [f"• {c}" for c in req["command"]]
    if req.get("error"):
        lines += ["", f"❌ {req['error']}"]
    if req.get("output"):
        lines += ["", "Sortie (fin) :", req["output"][-1500:]]
    text = "\n".join(lines)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"


def repo_tool() -> Path:
    # The code's own repository (not ULTRON_HUB_ROOT): the tool is part of the code, not of the data.
    return DEFAULT_ROOT / "deploy" / "user" / "ultron-user"


@lru_cache(maxsize=1)
def _load(path: str, mtime_ns: int, size: int) -> ModuleType:
    loader = importlib.machinery.SourceFileLoader("ultron_user", path)
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("ultron_user", loader))
    loader.exec_module(module)
    return module


def _spec() -> ModuleType:
    """The tool, reloaded when the file changed (mtime_ns + size): a long process (merge-lot) must not
    validate with a stale copy (todo #181)."""
    path = repo_tool()
    st = path.stat()
    return _load(str(path), st.st_mtime_ns, st.st_size)


def actions() -> tuple[str, ...]:
    return _spec().ACTIONS


def describe_actions() -> str:
    """The closed list, for the planners and the sessions (kept in line with the tool by a test)."""
    spec = _spec()
    pip = ", ".join(f"{k}=={v}" for k, v in spec.PIP_ALLOWED.items()) or "aucun paquet autorisé pour l'instant"
    keys = "; ".join(f"{k} = {'|'.join(v)}" for k, v in spec.CONFIG_KEYS.items())
    return (f"hermes_menu_add <{'|'.join(spec.PLUGIN_COMMANDS)}> (ajoute une commande du plugin au menu Telegram), "
            "hermes_menu_set <1 à 6 commandes du plugin, dans l'ordre> (remplace la liste priority du menu Telegram), "
            "user_unit_enable <ultron-hub-*.timer de deploy/systemd, ou le seul ultron-hub-dashboard.service> (lie et active le timer et son service), "
            "user_unit_disable <ultron-hub-*.timer lié depuis deploy/systemd, fichier éventuellement retiré du dépôt> "
            "(disable --now, retire les liens du timer et de son service vers le dépôt, daemon-reload), "
            f"user_unit_restart <{spec.GATEWAY}|ultron-hub-*.service|.timer de deploy/systemd>, "
            f"hermes_pip_install <paquet==version> ({pip}), hermes_config_set <clé> <valeur> ({keys}), "
            "montage_install (sans argument : paquets npm et navigateur de Remotion du montage vidéo, dans le dépôt), "
            "montage_tts_install (sans argument : voix de synthèse locale Piper du montage vidéo, venv à part, voix vérifiée par SHA-256), "
            "veille_lecture <veille> <dépôt de ~/workspace/projects/active> (la veille lit ce dépôt, en lecture seule), "
            "claude_hooks_sync (sans argument : ajoute à ~/.claude/settings.json les hooks Claude Code d'une liste fixe), "
            "ultron_poser (sans argument : pose la commande `ultron` du dépôt dans ~/.local/bin, idempotent), "
            f"restic_unlock_stale <heures, {spec.LOCK_MIN_H_FLOOR} au moins> <empreinte des verrous> (réflexe santé "
            "#161 : `restic unlock` seulement si les verrous du diagnostic sont périmés et leur processus absent)")


def preview(action: str, args: list[str]) -> list[str]:
    """Exactly what will run, one line per command or step, or InvalidRequestError."""
    spec = _spec()
    if not isinstance(action, str) or not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise InvalidRequestError("action and args must be strings")
    try:
        return spec.describe(action, list(args))
    except spec.Refused as exc:
        raise InvalidRequestError(str(exc)) from exc


def run(action: str, args: list[str]) -> dict[str, Any]:
    preview(action, args)  # refuse early
    try:
        proc = subprocess.run([PYTHON, "-I", str(repo_tool()), action, *args], capture_output=True, text=True,
                              timeout=RUN_TIMEOUT_S, env=ENV, cwd="/", stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": f"délai dépassé ({RUN_TIMEOUT_S} s)", "output": ""}
    lines = proc.stdout.strip().splitlines()
    try:
        return json.loads(lines[-1])
    except (IndexError, ValueError):
        detail = proc.stderr.strip()[-300:]
        return {"status": "error", "error": detail or f"sortie illisible (code {proc.returncode})", "output": ""}
