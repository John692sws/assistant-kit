"""Context gauge of a Claude Code session (todo #175): `ultron-hub esprit contexte --claude-hook`.

PostToolUse hook, script only (no AI). The measure and the rules live in `escouade/hooks.py` (standard library, shared
with the squad hooks); this module reads `config/contexte.yaml` and keeps the per-session tier in `var/contexte/`.
Any error (missing file, no usage, broken JSON) means silence: an empty answer and exit code 0.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

from ultron_hub.common.settings import Settings, project_root
from ultron_hub.escouade import hooks

SESSION_RE = re.compile(r"[A-Za-z0-9_-]{1,100}")
SQUAD_ENV = "ULTRON_ESCOUADE_DIR"  # a squad session has its own hook (hooks.py post)


def load_config(root: Path | None = None) -> dict[str, Any]:
    path = (root or project_root()) / "config" / "contexte.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        raw = None
    return hooks.context_config(raw if isinstance(raw, dict) else None)


def hook(settings: Settings, raw: str) -> dict[str, Any]:
    """Answer of the PostToolUse hook ({} = nothing to say)."""
    if os.environ.get(SQUAD_ENV) or os.environ.get("ULTRON_HUB_JOB"):
        return {}
    try:
        event = json.loads(raw or "{}")
    except ValueError:
        return {}
    if not isinstance(event, dict) or not SESSION_RE.fullmatch(str(event.get("session_id") or "")):
        return {}
    state = settings.var_dir / "contexte" / f"{event['session_id']}.json"
    text = hooks.context_check(event, load_config(settings.root), state)
    if not text:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}
