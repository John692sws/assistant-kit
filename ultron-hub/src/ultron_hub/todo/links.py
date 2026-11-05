"""What is attached to a todo outside the todo database: code branches (git).

Best effort: the todo list must still work if git is unreadable.
"""

from typing import Any

from ultron_hub import branches
from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import Settings

log = get_logger("todo.links")


def branch_map(settings: Settings) -> dict[int, dict[str, Any]]:
    try:
        return branches.branch_map(settings)
    except Exception:
        log.exception("branches unreadable")
        return {}


def badges(branch: dict[str, Any] | None) -> str:
    """Marker shown after a task: 🌿 code branch not yet merged."""
    return "🌿" if branch and branch.get("ahead") else ""
