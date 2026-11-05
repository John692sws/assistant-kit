"""The state a session declares of itself (todo #431): `ultron-hub session etat --maintenant … --ensuite … --attend … --bloque …`.

One small JSON file per session in `var/sessions-etat/<name>.json` (folder 0700, file 0600, atomic write), read back by
`session liste` as `declare` so a dashboard card says where the session stands without any AI. A state older than the session
(written before it was created, e.g. by a previous session of the same name) is ignored. No network.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ultron_hub.sessions import journal

FIELDS = ("maintenant", "ensuite", "attend", "bloque")
TEXT_MAX = 200


def dossier(settings: Any = None) -> Path:
    from ultron_hub.common.settings import project_root
    var = getattr(settings, "var_dir", None) or (project_root() / "var")
    return Path(var) / "sessions-etat"


def clean(text: str) -> str:
    """One line, control characters dropped, TEXT_MAX characters at most."""
    line = " ".join("".join(c if c.isprintable() else " " for c in str(text)).split())
    return line if len(line) <= TEXT_MAX else line[:TEXT_MAX - 1].rstrip() + "…"


def current_session(run: Any = subprocess.run) -> str:
    """Name of the tmux session we run in; SessionError outside tmux."""
    if not os.environ.get("TMUX"):
        raise journal.SessionError("hors tmux : indique --session <nom>")
    name = journal._tmux(["display-message", "-p", "#S"], run).strip()
    if not name:
        raise journal.SessionError("session tmux courante introuvable : indique --session <nom>")
    return name


def _path(name: str, base: Path) -> Path:
    if not journal.NAME_RE.fullmatch(name or ""):
        raise journal.SessionError(f"nom de session refusé : {name!r}")
    return base / f"{name}.json"


def _read(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        at = datetime.fromisoformat(str(raw["at"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    out = {k: (clean(raw[k]) or None) if isinstance(raw.get(k), str) else None for k in FIELDS}
    out["at"] = at.astimezone(timezone.utc).isoformat(timespec="seconds")
    return out


def lire(name: str, cree: float, base: Path) -> dict[str, Any] | None:
    """The declared state of a session, None when absent, unreadable, empty or older than the session (`cree`, epoch seconds)."""
    try:
        state = _read(_path(name, base))
    except journal.SessionError:
        return None
    if state is None or datetime.fromisoformat(state["at"]).timestamp() < cree:
        return None
    return state if any(state[k] for k in FIELDS) else None


def ecrire(name: str, base: Path, values: dict[str, str | None], efface: bool = False) -> dict[str, Any]:
    """Merge the given fields into the session's state (a field not given keeps its value); `efface` empties everything.

    With no field and no `efface`, nothing is written: the current state is returned."""
    path = _path(name, base)
    current = _read(path) or {**{k: None for k in FIELDS}, "at": None}
    given = {k: clean(v) or None for k, v in values.items() if k in FIELDS and v is not None}
    if not given and not efface:
        return {"session": name, **current}
    state = {k: None for k in FIELDS} if efface else {k: current[k] for k in FIELDS}
    state.update(given)
    state["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(base, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(state, ensure_ascii=False))
    os.replace(tmp, path)
    return {"session": name, **state}
