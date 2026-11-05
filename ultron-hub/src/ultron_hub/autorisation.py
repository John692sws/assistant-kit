"""The user's signed advance authorisation of automatic merges (todo #495), replacing the fixed week of todo #482.

While an authorisation is valid (`valide`), an automatic merge only refuses SECURITY (every repository, see
auto_merge.py); without one, back to the SENSITIVE rules. The user signs, at the dashboard, the target
"<perimeter>:<days>": perimeter = 16 hex of the SHA-256 of auto_merge.SECURITY + SECRET_NAMES (a changed list voids the
authorisation, the user signs what he sees), days in JOURS; it ends at the end of the Paris day today + days - 1.

var/autorisation-fusion.json = {perimetre, jours, jusqu_au, by, proof, at, sceau}, sceau = HMAC-SHA256 with the close
key (closing.load_key) of "ultron-hub-autorisation:v1:<perimetre>:<jusqu_au>:<by>:<proof>:<at>": a file written by
hand is refused. var/autorisation-fusion.jsonl = the history (grants and withdrawals); a proof already there is refused
(replay). Withdrawing needs no signature (it removes rights, like reopen).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import Settings
from ultron_hub.esprit import store as journal
from ultron_hub.todo import closing

log = get_logger("autorisation")

PARIS = ZoneInfo("Europe/Paris")
JOURS = (1, 3, 7, 14)
FILE_NAME = "autorisation-fusion.json"
HISTORY_NAME = "autorisation-fusion.jsonl"
FIELDS = ("perimetre", "jours", "jusqu_au", "by", "proof", "at")


def fingerprint(security: tuple[str, ...], secret_names: tuple[str, ...]) -> str:
    text = "\n".join(security) + "\n--\n" + "\n".join(secret_names)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def perimetre() -> str:
    """The current perimeter (imported late: auto_merge imports this module)."""
    from ultron_hub import auto_merge
    return fingerprint(auto_merge.SECURITY, auto_merge.SECRET_NAMES)


def today(now: float | None = None) -> date:
    return datetime.fromtimestamp(time.time() if now is None else now, PARIS).date()


def fin(jours: int, now: float | None = None) -> str:
    """Last day included (Paris time) of an authorisation of `jours` days given now."""
    return (today(now) + timedelta(days=jours - 1)).isoformat()


def cible(perim: str, jours: int) -> str:
    return f"{perim}:{jours}"


def _path(settings: Settings):
    return settings.var_dir / FILE_NAME


def _history(settings: Settings):
    return settings.var_dir / HISTORY_NAME


def _seal(key: bytes, entry: dict) -> str:
    msg = "ultron-hub-autorisation:v1:{perimetre}:{jusqu_au}:{by}:{proof}:{at}".format(**entry)
    return hmac.new(key, msg.encode(), hashlib.sha256).hexdigest()


def valide(settings: Settings, now: float | None = None) -> dict[str, Any] | None:
    """The valid authorisation, or None (absent, unreadable, bad seal, no key, other perimeter, past date)."""
    try:
        entry = json.loads(_path(settings).read_text(encoding="utf-8"))
        if not isinstance(entry, dict) or not all(isinstance(entry.get(f), (str, int)) for f in FIELDS):
            return None
        seal = entry.get("sceau")
        if not isinstance(seal, str) or not hmac.compare_digest(seal, _seal(closing.load_key(), entry)):
            log.warning("autorisation: bad seal, ignored")
            return None
        if entry["perimetre"] != perimetre():
            return None
        if date.fromisoformat(str(entry["jusqu_au"])) < today(now):
            return None
    except (OSError, ValueError, closing.CloseRefused):
        return None
    return {f: entry[f] for f in ("perimetre", "jours", "jusqu_au", "by", "at")}


def _entries(settings: Settings) -> list[dict]:
    try:
        lines = _history(settings).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def proof_used(settings: Settings, proof: str) -> bool:
    return any(e.get("proof") == proof for e in _entries(settings))


def _append(settings: Settings, entry: dict) -> None:
    path = _history(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def etat(settings: Settings, now: float | None = None) -> dict[str, Any]:
    """Read only: the valid authorisation (or none), the current perimeter and the end of each possible duration."""
    v = valide(settings, now)
    return {"valide": v is not None, "jusqu_au": v and v["jusqu_au"], "by": v and v["by"], "perimetre": perimetre(),
            "jours": list(JOURS), "fins": [{"jours": j, "jusqu_au": fin(j, now)} for j in JOURS]}


def check(jours: int, perim: str) -> str:
    """The signed target, or InvalidRequestError if the duration is unknown or the perimeter is not the current one."""
    if jours not in JOURS:
        raise InvalidRequestError(f"durée refusée : {', '.join(map(str, JOURS))} jours")
    if perim != perimetre():
        raise InvalidRequestError("périmètre différent de la liste de sécurité actuelle : relis l'état et signe à nouveau")
    return cible(perim, jours)


def donner(settings: Settings, jours: int, perim: str, by: dict, now: float | None = None) -> dict[str, Any]:
    """Write the sealed authorisation (the caller has checked the user's proof on `check(jours, perim)`)."""
    check(jours, perim)
    entry = {"perimetre": perim, "jours": jours, "jusqu_au": fin(jours, now), "by": str(by.get("by") or ""),
             "proof": str(by.get("proof") or ""), "at": int(time.time() if now is None else now)}
    entry["sceau"] = _seal(closing.load_key(), entry)
    path = _path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(entry, fh, ensure_ascii=False)
    os.replace(tmp, path)
    _append(settings, {"type": "don", **{k: entry[k] for k in FIELDS}})
    log.warning("autorisation given until %s by %s", entry["jusqu_au"], entry["by"])
    journal.record(settings, "ultron-hub", "fusion",
                   f"Autorisation de fusion donnée jusqu'au {entry['jusqu_au']} ({entry['by']})")
    return {k: entry[k] for k in ("perimetre", "jours", "jusqu_au", "by", "at")}


def retirer(settings: Settings, by: str = "cli", now: float | None = None) -> dict[str, Any]:
    """Remove the authorisation (no signature: it removes rights)."""
    path = _path(settings)
    existed = path.exists()
    path.unlink(missing_ok=True)
    _append(settings, {"type": "retrait", "by": by, "at": int(time.time() if now is None else now), "existait": existed})
    log.warning("autorisation withdrawn by %s", by)
    journal.record(settings, "ultron-hub", "fusion", "Autorisation de fusion retirée" + ("" if existed else " (aucune)"))
    return {"retiree": existed}
