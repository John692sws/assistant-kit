"""Focus of the week (todo #165): at most two domains (systeme, business, perso), chosen by the user, read by every session.

`~/ultron/esprit/focus.json` = {"domaines": [...], "depuis": "<iso>", "par": "<by>"}. Written only here (lock + atomic
replace of `store`). The old format {"chantiers": [...]} is read through each chantier's `Domaine :` (todo #186). Read best effort: a missing or unreadable file is « no focus », never an error.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.settings import Settings
from ultron_hub.esprit import store

FILE = "focus.json"
MAX = 2
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
BY_RE = re.compile(r"[a-z0-9:_-]{1,40}")
ASK_TEXT = ("🧭 Nouvelle semaine : quel focus ?\n{actuel}\n"
            "Choisis 1 ou 2 domaines : /plus puis 🧭 Focus. Sans réponse, le focus reste le même.")


def _keys() -> tuple[str, ...]:
    from ultron_hub.chantier.core import DOMAINES  # lazy: chantier does not import esprit, keep it that way
    return tuple(d[0] for d in DOMAINES)


def domaines_info() -> list[dict[str, str]]:
    """The domains one can choose (key, icon, title), same list as /chantiers."""
    from ultron_hub.chantier.core import DOMAINES
    return [{"cle": k, "icone": i, "titre": t} for k, i, t in DOMAINES]


def _domaine_de_chantier(settings: Settings, sujet: str) -> str:
    """Domain of a chantier from its `Domaine :` header; "" when unknown or unreadable (old focus.json only)."""
    from ultron_hub.chantier import core
    try:
        path = core.sources().prompts / f"{sujet}.md"
        return core.parse_prompt(path.read_text(encoding="utf-8"), sujet)["domaine"]
    except (OSError, ValueError, InvalidRequestError):
        return ""


def read(settings: Settings) -> dict[str, Any] | None:
    """{"domaines": [...], "depuis", "par"}. The old format {"chantiers": [...]} is converted on reading (never rewritten)."""
    path = store.root(settings) / FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        keys = _keys()
        if "domaines" in data:
            found = [n for n in data.get("domaines") or [] if isinstance(n, str)]
        else:
            found = [_domaine_de_chantier(settings, n) for n in data.get("chantiers", [])
                     if isinstance(n, str) and NAME_RE.fullmatch(n)]
        names = list(dict.fromkeys(n for n in found if n in keys))[:MAX]
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    if not names:
        return None
    return {"domaines": names, "depuis": str(data.get("depuis") or ""), "par": str(data.get("par") or "")}


def dans_le_focus(data: dict[str, Any] | None, domaine: str) -> bool:
    """A chantier is in the focus when its domain is (an unranged chantier, domain "", never is)."""
    return bool(data and domaine and domaine in data.get("domaines", []))


def parse(names: list[str]) -> list[str]:
    chosen: list[str] = []
    for name in names:
        if name not in _keys():
            raise InvalidRequestError(f"domaine refusé : {name!r} (au choix : {', '.join(_keys())})")
        if name not in chosen:
            chosen.append(name)
    if not 1 <= len(chosen) <= MAX:
        raise InvalidRequestError(f"le focus compte 1 ou {MAX} domaines (reçu : {len(chosen)})")
    return chosen


def write(settings: Settings, names: list[str], by: str) -> dict[str, Any]:
    if not BY_RE.fullmatch(by or ""):
        raise InvalidRequestError("--by must match [a-z0-9:_-]{1,40}")
    chosen = parse(names)
    folder = store._existing(settings)
    data = {"domaines": chosen, "depuis": store.now_iso(settings), "par": by}
    with store._locked(folder):
        store._write_atomic(folder / FILE, json.dumps(data, ensure_ascii=False) + "\n")
    store.record(settings, "ultron-hub", "decision", f"Focus de la semaine : {', '.join(chosen)}")
    return data


def line(data: dict[str, Any] | None) -> str:
    if not data:
        return "🧭 Focus de la semaine : aucun (au plus 2 domaines, à choisir dans /plus → 🧭 Focus)."
    since = data.get("depuis", "")[:10]
    day = f" (depuis le {since[8:10]}/{since[5:7]})" if len(since) == 10 else ""
    return f"🧭 Focus de la semaine : {' + '.join(data['domaines'])}{day}. Rien hors focus sans le demander."


def ask_text(data: dict[str, Any] | None) -> str:
    actuel = f"Focus actuel : {' + '.join(data['domaines'])}." if data else "Aucun focus pour l'instant."
    return ASK_TEXT.format(actuel=actuel)
