"""Shared mind of Ultron (todo #47): one folder read by Hermes and Claude Code.

    <esprit_dir>/profil.md          facts about the user (only the user's ✅ adds to it)
    <esprit_dir>/charte.md          identity "Ultron", tone and common rules
    <esprit_dir>/journal.jsonl      one entry per line: {date, source, type, todo, texte}
    <esprit_dir>/archives/          journal of previous months (journal-YYYY-MM.jsonl)
    <esprit_dir>/propositions.jsonl facts proposed by agents, waiting for the user's decision

Only ultron-hub writes here (lock + atomic replace). The journal holds titles and actions, never the
content of a mail or a document: entries are one bounded line.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from ultron_hub.common.errors import InvalidRequestError, UltronHubError
from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import Settings

log = get_logger("esprit")

PROFIL = "profil.md"
CHARTE = "charte.md"
JOURNAL = "journal.jsonl"
PROPOSITIONS = "propositions.jsonl"
ARCHIVES = "archives"
LOCK = ".lock"
CHARTE_TEMPLATE = Path("deploy") / "esprit" / "charte.md"  # relative to the project root
ADDED_HEADING = "## Ajouts validés"

TEXT_MAX = 300          # one journal entry (a line, not a document)
FACT_MAX = 300          # one proposed fact
TAIL_DEFAULT = 20
TAIL_MAX = 200
SEARCH_MAX = 50
HOOK_JOURNAL_MAX = 8    # journal lines given to Claude Code at session start
HOOK_NOISE_PREFIXES = ("📜 ", "✍️ ")  # dashboard: fil/notification marked read, characters sent to a session
CONTEXT_MAX = 3900      # Hermes caps one system prompt section at 4000 chars
_SOURCE_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,39}")
_TYPE_RE = re.compile(r"[a-z][a-z0-9_.-]{0,29}")
PROPOSAL_STATUSES = ("en_attente", "acceptee", "refusee")


class EspritMissing(UltronHubError):
    """The shared-mind folder does not exist yet (`ultron-hub esprit init`)."""


def root(settings: Settings) -> Path:
    return settings.esprit_dir


def now_iso(settings: Settings) -> str:
    try:
        tz = ZoneInfo(settings.todo_timezone)
    except Exception:
        tz = None
    return datetime.now(tz).isoformat(timespec="seconds")


def one_line(text: Any, limit: int) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[:limit - 1] + "…"


# Zero-width, BOM and bidi-control characters (same list as Hermes' threat_patterns): they hide or reorder text.
INVISIBLE_CHARS = frozenset(
    "\u200b\u200c\u200d\u2060\u2062\u2063\u2064\ufeff"
    "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def strip_invisible(text: Any) -> tuple[str, int]:
    """Text without invisible characters, and how many were removed."""
    raw = str(text or "")
    clean = "".join(c for c in raw if c not in INVISIBLE_CHARS)
    return clean, len(raw) - len(clean)


def _existing(settings: Settings) -> Path:
    path = root(settings)
    if not path.is_dir():
        raise EspritMissing(f"esprit absent : {path} (lancer `ultron-hub esprit init`)")
    return path


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    fd = os.open(path / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a torn line never hides the others
        if isinstance(row, dict):
            rows.append(row)
    return rows


# -- init ---------------------------------------------------------------------------

def init(settings: Settings, profil_from: Path | None = None) -> dict[str, Any]:
    """Creates the folder (0700) and the missing files; an existing file is never overwritten."""
    path = root(settings)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    created, kept = [], []
    sources = {PROFIL: profil_from, CHARTE: settings.root / CHARTE_TEMPLATE}
    for name, source in sources.items():
        target = path / name
        if target.exists():
            kept.append(name)
            continue
        if source is None:
            text = "# Profil de l'utilisateur\n\n"
        elif not Path(source).is_file():
            raise InvalidRequestError(f"fichier source introuvable : {source}")
        else:
            text = Path(source).read_text(encoding="utf-8")
        _write_atomic(target, text)
        created.append(name)
    for name in (JOURNAL, PROPOSITIONS):
        target = path / name
        if not target.exists():
            os.close(os.open(target, os.O_WRONLY | os.O_CREAT, 0o600))
            created.append(name)
    (path / ARCHIVES).mkdir(mode=0o700, exist_ok=True)
    return {"dir": str(path), "created": created, "kept": kept}


# -- journal ------------------------------------------------------------------------

def _rotate(path: Path, month: str) -> None:
    """Moves last month's journal to archives/ when the first entry of a new month arrives."""
    journal = path / JOURNAL
    rows = _read_jsonl(journal)
    first = next((r for r in rows if isinstance(r.get("date"), str)), None)
    if first is None or first["date"][:7] == month:
        return
    archive = path / ARCHIVES / f"journal-{first['date'][:7]}.jsonl"
    archive.parent.mkdir(mode=0o700, exist_ok=True)
    with open(archive, "a", encoding="utf-8") as fh:  # several moves in one month append, never overwrite
        fh.write(journal.read_text(encoding="utf-8"))
    _write_atomic(journal, "")


def add(settings: Settings, source: str, kind: str, text: str, todo: int | None = None) -> dict[str, Any]:
    if _SOURCE_RE.fullmatch(source or "") is None:
        raise InvalidRequestError("source invalide (ex. hermes, claude-code, ultron-hub, mission)")
    if _TYPE_RE.fullmatch(kind or "") is None:
        raise InvalidRequestError("type invalide (ex. todo, mission, fusion, resume, decision)")
    line = one_line(text, TEXT_MAX)
    if not line:
        raise InvalidRequestError("texte vide")
    if todo is not None and (not isinstance(todo, int) or todo <= 0):
        raise InvalidRequestError("numéro de todo invalide")
    path = _existing(settings)
    entry = {"date": now_iso(settings), "source": source, "type": kind, "todo": todo, "texte": line}
    with _locked(path):
        _rotate(path, entry["date"][:7])
        fd = os.open(path / JOURNAL, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def record(settings: Settings, source: str, kind: str, text: str, todo: int | None = None) -> None:
    """Best effort for automatic events: a journal problem never breaks the action that happened."""
    try:
        if root(settings).is_dir():
            add(settings, source, kind, text, todo)
    except Exception:
        log.warning("journal: entry not recorded (%s/%s)", source, kind, exc_info=True)


def _all_entries(path: Path) -> list[dict]:
    rows = []
    archives = path / ARCHIVES
    if archives.is_dir():
        for archive in sorted(archives.glob("journal-*.jsonl")):
            rows += _read_jsonl(archive)
    return rows + _read_jsonl(path / JOURNAL)


def tail(settings: Settings, limit: int = TAIL_DEFAULT) -> list[dict]:
    limit = max(1, min(int(limit), TAIL_MAX))
    path = _existing(settings)
    rows = _read_jsonl(path / JOURNAL)
    if len(rows) < limit:
        rows = _all_entries(path)
    return rows[-limit:]


def search(settings: Settings, query: str, limit: int = SEARCH_MAX) -> list[dict]:
    needle = (query or "").strip().casefold()
    if not needle:
        raise InvalidRequestError("recherche vide")
    hits = [r for r in _all_entries(_existing(settings))
            if needle in " ".join(str(r.get(k) or "") for k in ("texte", "source", "type")).casefold()]
    return hits[::-1][:max(1, min(int(limit), SEARCH_MAX))]


def format_entry(entry: dict) -> str:
    todo = f" #{entry['todo']}" if entry.get("todo") else ""
    date = str(entry.get("date") or "")[:16].replace("T", " ")
    return f"{date} [{entry.get('source')}/{entry.get('type')}{todo}] {entry.get('texte')}"


# -- profile and proposals ----------------------------------------------------------

def read_doc(settings: Settings, name: str) -> str:
    path = _existing(settings) / name
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def proposals(settings: Settings, status: str | None = "en_attente") -> list[dict]:
    rows = _read_jsonl(_existing(settings) / PROPOSITIONS)
    return [r for r in rows if status is None or r.get("statut") == status]


def _save_proposals(path: Path, rows: list[dict]) -> None:
    _write_atomic(path / PROPOSITIONS, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def propose(settings: Settings, fact: str, source: str) -> dict[str, Any]:
    if _SOURCE_RE.fullmatch(source or "") is None:
        raise InvalidRequestError("source invalide (ex. agent:hermes, claude-code)")
    clean, removed = strip_invisible(fact)
    text = one_line(clean, FACT_MAX)
    if not text:
        raise InvalidRequestError("fait vide")
    path = _existing(settings)
    with _locked(path):
        rows = _read_jsonl(path / PROPOSITIONS)
        same = next((r for r in rows if r.get("statut") == "en_attente" and r.get("texte") == text), None)
        if same is not None:
            return {**same, "doublon": True}
        row = {"id": max((int(r.get("id") or 0) for r in rows), default=0) + 1, "date": now_iso(settings),
               "source": source, "texte": text, "statut": "en_attente"}
        if removed:
            row["invisible_retire"] = removed
        _save_proposals(path, rows + [row])
    return row


def proof_used(settings: Settings, proof: str) -> bool:
    return any(r.get("proof") == proof for r in proposals(settings, None))


def decide(settings: Settings, proposal_id: int, accept: bool, by: dict, text: str | None = None) -> dict:
    """✅ (accept, optionally with the user's corrected text) appends the fact to profil.md; ❌ only records it."""
    path = _existing(settings)
    with _locked(path):
        rows = _read_jsonl(path / PROPOSITIONS)
        row = next((r for r in rows if r.get("id") == proposal_id), None)
        if row is None:
            raise InvalidRequestError(f"proposition {proposal_id} introuvable")
        if row.get("statut") != "en_attente":
            raise InvalidRequestError(f"proposition {proposal_id} déjà traitée ({row.get('statut')})")
        final = one_line(strip_invisible(text)[0], FACT_MAX) if text is not None else row["texte"]
        if not final:
            raise InvalidRequestError("fait vide")
        row.update(statut="acceptee" if accept else "refusee", decided_at=now_iso(settings), **by)
        if accept:
            row["texte_final"] = final
            _append_fact(path / PROFIL, final, row["decided_at"][:10], row["source"])
        _save_proposals(path, rows)
    verb = "ajouté au profil" if accept else "refusé"
    record(settings, "ultron-hub", "profil", f"Fait {verb} (proposition {proposal_id}) : {row.get('texte_final', row['texte'])}")
    return row


def _append_fact(profil: Path, fact: str, day: str, source: str) -> None:
    text = profil.read_text(encoding="utf-8") if profil.is_file() else "# Profil de l'utilisateur\n"
    if ADDED_HEADING not in text:
        text = text.rstrip("\n") + f"\n\n{ADDED_HEADING}\n"
    _write_atomic(profil, text.rstrip("\n") + f"\n- {fact} ({day}, proposé par {source})\n")


# -- context for Hermes and Claude Code --------------------------------

def _bounded(text: str, limit: int = CONTEXT_MAX) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit - 30].rstrip() + "\n[… tronqué]"


def persona(settings: Settings) -> dict[str, str]:
    """Profile and charter (empty dict when the folder does not exist yet)."""
    if not root(settings).is_dir():
        return {}
    return {"profil": _bounded(read_doc(settings, PROFIL)), "charte": _bounded(read_doc(settings, CHARTE))}


def _is_noise(entry: dict) -> bool:
    """Read-only or plumbing dashboard action (marked read, text sent to a session): no use at session start."""
    return (entry.get("source") == "dashboard" and entry.get("type") == "action"
            and str(entry.get("texte") or "").startswith(HOOK_NOISE_PREFIXES))


def journal_context(settings: Settings, limit: int = TAIL_DEFAULT, skip_noise: bool = False) -> str:
    if not root(settings).is_dir():
        return ""
    rows = tail(settings, TAIL_MAX if skip_noise else limit)
    if skip_noise:  # the last `limit` entries AFTER filtering
        rows = [e for e in rows if not _is_noise(e)][-max(1, int(limit)):]
    lines = [format_entry(e) for e in rows]
    if not lines:
        return ""
    return _bounded("Journal commun d'Ultron (dernières entrées, la plus récente en bas) :\n" + "\n".join(lines))
