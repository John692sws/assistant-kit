"""What the dashboard's home screen says of each ultron session (todo #281), added to `session liste`.

For one session: its chantier (`ultron-<chantier>-JJ-MM-HHhMM`, todo #284; None for the Main session), the sub-agents still
running, whether it works right now (journal written less than WORKING_S ago) and whether it waits for the user's answer
(its last entry is the agent asking: a question mark in the last lines of its reply, or a question tool). Todo #406 adds the
context gauge in % (same measure as the hook, `escouade.hooks`), the running sub-agents in detail and the last sentence of
the agent's last reply. No AI: a script.
Only the journal found by `journal.find_journal` is read (same guard); an unreadable journal leaves the fields unknown.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Callable

from ultron_hub.escouade import hooks
from ultron_hub.esprit import contexte
from ultron_hub.sessions import journal

CHANTIER_RE = re.compile(r"ultron-(?P<chantier>[a-z0-9][a-z0-9-]{0,39})-[0-9]{2}-[0-9]{2}-[0-9]{2}h[0-9]{2}(?:-[0-9]{1,3})?")
WORKING_S = 90  # Claude Code writes its journal at each step of a turn
QUESTION_TOOLS = ("AskUserQuestion",)
TAIL_LINES = 3  # « …, ou bien B ? » followed by a list of options
MARKUP = " \t*_`>)]»"
SENTENCE_MAX = 160
SCAN_ENTRIES = 40  # entries read to find the last reply (tools and sub-agent events can follow it)
SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
MD_MARK = re.compile(r"[*_`#>|~]+|^\s*(?:[-+]|\d+[.)])\s+", re.MULTILINE)


def chantier_of(name: str) -> str | None:
    found = CHANTIER_RE.fullmatch(name or "")
    chantier = found.group("chantier") if found else None
    return None if chantier == journal.MAIN else chantier


def waits(entry: dict[str, Any] | None) -> bool:
    """True when the last entry of the journal is the agent asking something."""
    if not entry:
        return False
    if entry.get("k") == "outil":
        return entry.get("outil") in QUESTION_TOOLS
    if entry.get("k") != "reponse":
        return False
    lines = [ln.strip().rstrip(MARKUP) for ln in str(entry.get("texte") or "").splitlines() if ln.strip()]
    return any(ln.endswith("?") for ln in lines[-TAIL_LINES:])


def last_sentence(entries: list[dict[str, Any]]) -> str | None:
    """Last sentence (SENTENCE_MAX characters at most, no markdown) of the last agent reply, None when there is none."""
    for entry in reversed(entries):
        if entry.get("k") != "reponse":
            continue
        text = MD_MARK.sub(" ", MD_LINK.sub(r"\1", str(entry.get("texte") or "")))
        text = " ".join(text.split())
        if not text:
            continue
        sentence = SENTENCE_END.split(text)[-1].strip()
        return sentence if len(sentence) <= SENTENCE_MAX else sentence[:SENTENCE_MAX - 1].rstrip() + "…"
    return None


def context_pct(path: Any, cfg: dict[str, Any] | None = None) -> int | None:
    """Context window used, in %, by the gauge of the hook (`escouade.hooks`); None when the journal says nothing."""
    usage = hooks.last_usage(str(path))
    if usage is None:
        return None
    return int(hooks.context_percent(usage[0], usage[1], cfg or contexte.load_config()))


def agents_of(running: Any) -> list[dict[str, Any]]:
    return [{"nom": a.get("description") or "sous-agent", "modele": a.get("model") or None, "depuis": a.get("depuis")}
            for a in running or [] if isinstance(a, dict)]


def digest(name: str, find: Callable[[str], Any] = journal.find_journal, read: Callable[..., dict[str, Any]] = journal.read_entries,
           now: Callable[[], float] = time.time, gauge: Callable[[Any], int | None] = context_pct) -> dict[str, Any]:
    out: dict[str, Any] = {"chantier": chantier_of(name), "sous_agents": None, "attend": False, "travaille": False,
                           "contexte": None, "agents": [], "derniere_phrase": None}
    try:
        path = find(name)
        data = read(path, SCAN_ENTRIES)
    except (journal.SessionError, OSError, ValueError):
        return out
    entries = data.get("entrees") or []
    out["sous_agents"] = len(data.get("sous_agents") or [])
    out["agents"] = agents_of(data.get("sous_agents"))
    out["attend"] = waits(entries[-1] if entries else None)
    out["travaille"] = works(data.get("maj"), out["attend"], now)
    out["derniere_phrase"] = last_sentence(entries)
    try:
        out["contexte"] = gauge(path)
    except Exception:  # noqa: BLE001 - an unreadable gauge leaves the field unknown
        pass
    return out


def works(maj: Any, attend: bool, now: Callable[[], float] = time.time) -> bool:
    """The session works right now: its journal was written less than WORKING_S ago and it does not wait for an answer."""
    try:
        written = datetime.fromisoformat(str(maj)).timestamp()
    except ValueError:
        return False
    return not attend and now() - written < WORKING_S
