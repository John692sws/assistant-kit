"""Progress file of a merge (todo #140): `var/merge/progress.json`, read by the Hermes plugin every few seconds to
edit the Telegram message while 🔀 / 🔀 Tout fusionner runs.

Only todo numbers, step names and times: no external data, no secret. Written atomically, mode 0600. Best effort:
a write that fails is ignored, the merge always goes first.
"""

import json
import os
import time
from pathlib import Path

from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import Settings

log = get_logger("merge_progress")

STEPS = ("essai", "tests", "fusion", "cloture")
SUIVI_ENV = "ULTRON_HUB_SUIVI"  # = dashboard.fond.SUIVI_ENV: the dashboard's background job running this merge (todo #267)


def path(settings: Settings) -> Path:
    return settings.var_dir / "merge" / "progress.json"


class Progress:
    """State of one merge command (one todo, or a lot): which todo, which step since when, which are done."""

    def __init__(self, settings: Settings, ids: list[int]):
        self.settings = settings
        self.state: dict = {"v": 1, "ids": list(ids), "todo": None, "step": None, "since": None, "done": {},
                            "restart": False, "updated": time.time(),
                            "suivi": os.environ.get(SUIVI_ENV) or None}
        self._todo_since: dict[int, float] = {}

    def step(self, todo_id: int, step: str) -> None:
        now = time.time()
        self._todo_since.setdefault(todo_id, now)
        self.state.update(todo=todo_id, step=step, since=now)
        self._write()

    def done(self, todo_id: int) -> None:
        self.state["done"][str(todo_id)] = round(time.time() - self._todo_since.get(todo_id, time.time()), 1)
        self.state.update(todo=None, step=None, since=None)
        self._write()

    def actions(self) -> None:
        """The lot launches its after-merge actions (todo #267: shown by the dashboard)."""
        self.state.update(todo=None, step="actions", since=time.time())
        self._write()

    def restart(self) -> None:
        self.state.update(todo=None, step=None, since=None, restart=True)
        self._write()

    def _write(self) -> None:
        self.state["updated"] = time.time()
        try:
            target = path(self.settings)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = target.with_name(f".progress-{os.getpid()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh)
            os.replace(tmp, target)
        except Exception as exc:  # never fail or slow down a merge for its display
            log.debug("progress not written: %s", exc)


def merged_ids(settings: Settings) -> list[int]:
    """Todos merged by the running command (for the message after the gateway restart); [] if unknown."""
    try:
        state = json.loads(path(settings).read_text(encoding="utf-8"))
        return sorted(int(t) for t in state.get("done") or {})
    except Exception:
        return []
