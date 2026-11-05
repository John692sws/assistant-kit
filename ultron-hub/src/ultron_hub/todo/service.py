"""Todo rules shared by the CLI and the Hermes plugin: input parsing, digest, notification."""

import hashlib
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Literal
from zoneinfo import ZoneInfo

from ultron_hub.common.errors import ConfigError, InvalidRequestError, UltronHubError
from ultron_hub.common.logging import get_logger
from ultron_hub.escouade import core as escouade_core
from ultron_hub.todo.store import CHANTIER_MAX, CLAIR_MAX, NATURES, PRIORITIES, Task, validate_clair

log = get_logger("todo")

# ---------------------------------------------------------------------------
# Title length rules (spec: spec-titre-long-todo)
# ---------------------------------------------------------------------------

TITLE_MAX_LEN = 60       # Unicode code points
NOTES_MAX_LEN = 4000     # upper bound on notes field
Mode = Literal["reject", "truncate", "summarize"]


class TitleTooLongError(ValueError):
    def __init__(self, length: int, limit: int):
        super().__init__(f"title is {length} chars, limit is {limit}")
        self.length = length
        self.limit = limit


class EmptyTitleError(ValueError):
    pass


class NotesTooLongError(ValueError):
    pass


@dataclass(frozen=True)
class TitleResult:
    title: str
    notes: str | None          # new notes value, None = unchanged
    source: Literal["as_is", "truncated", "llm"]


def normalize_title(raw: str) -> str:
    """Newlines/tabs -> space, collapse whitespace, strip."""
    return " ".join(raw.split())


def truncate_title(text: str, max_len: int = TITLE_MAX_LEN) -> str:
    """Cut on a word boundary, append '…', total length <= max_len."""
    if len(text) <= max_len:
        return text
    words = text.split()
    result = ""
    for word in words:
        candidate = (result + " " + word).strip() if result else word
        if len(candidate) + 1 <= max_len:   # +1 for the '…'
            result = candidate
        else:
            break
    if result and len(result) + 1 <= max_len:
        return result + "…"
    # Single very long word: hard cut at max_len-1 chars + '…'
    return text[:max_len - 1] + "…"


def append_notes(existing: str | None, extra: str) -> str:
    """Append below existing notes (blank line), never overwrite; skip if already present.

    Raises NotesTooLongError if result > NOTES_MAX_LEN.
    """
    existing_text = (existing or "").rstrip()
    extra_stripped = extra.rstrip()
    # Skip if already present at the end of notes
    if existing_text.endswith(extra_stripped):
        result = existing_text
    elif existing_text:
        result = existing_text + "\n\n" + extra_stripped
    else:
        result = extra_stripped
    if len(result) > NOTES_MAX_LEN:
        raise NotesTooLongError(f"notes would exceed {NOTES_MAX_LEN} characters")
    return result


def resolve_title(
    raw: str,
    existing_notes: str | None = None,
    mode: Mode = "reject",
    summarizer: Callable[[str], str] | None = None,
) -> TitleResult:
    """Enforce TITLE_MAX_LEN, applying the chosen mode when exceeded."""
    text = normalize_title(raw)
    if not text:
        raise EmptyTitleError("title is empty after normalization")
    if len(text) <= TITLE_MAX_LEN:
        return TitleResult(text, None, "as_is")
    if mode == "reject":
        raise TitleTooLongError(len(text), TITLE_MAX_LEN)
    full = raw.strip()   # keep original line breaks in notes
    notes = append_notes(existing_notes, full)
    if mode == "summarize" and summarizer:
        try:
            t = normalize_title(summarizer(text[:NOTES_MAX_LEN]))
            if t and len(t) <= TITLE_MAX_LEN:
                return TitleResult(t, notes, "llm")
        except Exception:
            log.warning("todo-titre failed, truncating", exc_info=True)
    return TitleResult(truncate_title(text), notes, "truncated")


NATURE_ICONS = {"chantier": "🎯", "fond": "🌱"}
CLAIR_PER_PASS = 5   # todos completed by one run of the daily digest
CLAIR_BUDGET_S = 60  # no new generation after this, so the digest stays within its systemd timeout


def parse_nature(value: str) -> str:
    value = value.strip().lower()
    if value not in NATURES:
        raise InvalidRequestError(f"nature must be one of {', '.join(NATURES)}")
    return value


def nature_journal_text(task: Task) -> str:
    return (f"Todo passée en chantier : {task.title}" if task.nature == "chantier"
            else f"Todo mise en fond : {task.title}")


def known_chantiers(folder: Path | None = None) -> list[str]:
    """The subjects that exist: `<s>.md` in the prompts folder, or archived in its `termines/` folder."""
    folder = folder or escouade_core.prompts_dir()
    return sorted(set(escouade_core.list_subjects(folder)) | set(escouade_core.list_subjects(folder / "termines")))


def parse_chantier(value: str, must_exist: bool = True) -> str:
    """A chantier name: an existing subject (a typo is refused, not filed), or just well-formed to filter on it."""
    value = value.strip()
    if not value or len(value) > CHANTIER_MAX:
        raise InvalidRequestError(f"chantier : un nom de 1 à {CHANTIER_MAX} caractères est attendu")
    escouade_core.check_name(value)
    if must_exist and value not in known_chantiers():
        folder = escouade_core.prompts_dir()
        raise InvalidRequestError(f"chantier inconnu : {value!r} (aucun {value}.md dans {folder} ni dans "
                                  f"{folder / 'termines'})")
    return value


def chantier_journal_text(task: Task) -> str:
    return (f"Todo rattachée au chantier {task.chantier} : {task.title}" if task.chantier
            else f"Todo sortie de son chantier : {task.title}")


# Todo -> chantier, decided by the user on 2026-10-16 (spec v3 §3.1). None = stays out of any chantier.
RATTACHEMENT: dict[int, str | None] = {
    **dict.fromkeys((86, 87, 88, 89, 90), "agent-ultron"),
    **dict.fromkeys((81, 82, 83, 84), "al-kitab"),
    **dict.fromkeys((91, 92, 93, 94, 80), "video-neutre"),
    **dict.fromkeys((100, 101), "escouade-autonome"),
    48: "chantier-4",
    79: "montage-video",
    **dict.fromkeys((4, 16, 17, 33, 34, 35, 49), None),
}


def propose_rattachement(tasks: list[Task], known: list[str],
                         table: dict[int, str | None] | None = None) -> list[dict]:
    """The changes the table asks for: open tasks whose chantier differs, towards known subjects only."""
    table = RATTACHEMENT if table is None else table
    lines = []
    for task in sorted(tasks, key=lambda t: t.id):
        if task.status != "todo" or task.id not in table:
            continue
        target = table[task.id]
        if target is not None and target not in known:
            continue
        if task.chantier != target:
            lines.append({"id": task.id, "titre": task.title, "de": task.chantier, "vers": target})
    return lines


def rattachement_fingerprint(lines: list[dict]) -> str:
    """8 hex chars over the canonical proposal (each line carries the current state of its todo)."""
    canonical = json.dumps(lines, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:8]


def clean_clair(raw: str) -> str:
    """Model output -> a valid "En clair" line (one line, cut on a word boundary if too long)."""
    text = " ".join(raw.split()).strip(" \"«»")
    return truncate_title(text, CLAIR_MAX) if len(text) > CLAIR_MAX else text


def fill_clair(store, tasks: list[Task], generate: Callable[[Task], str], limit: int = CLAIR_PER_PASS,
               budget_s: float = CLAIR_BUDGET_S) -> list[int]:
    """Write a generated "En clair" for open tasks that have none, at most `limit`.

    Best effort: a failing generation is logged and skipped, it never raises. Returns the ids filled.
    """
    filled: list[int] = []
    started = time.monotonic()
    for task in tasks:
        if len(filled) >= limit or time.monotonic() - started > budget_s:
            break
        if task.clair or task.status != "todo":
            continue
        try:
            clair = validate_clair(clean_clair(generate(task)))
            if clair:
                store.update(task.id, by="haiku", clair=clair)
                filled.append(task.id)
        except Exception:
            log.warning("todo=%s clair not generated", task.id, exc_info=True)
    return filled


PRIORITY_ALIASES = {"haute": "high", "normale": "normal", "basse": "low"}
PRIORITY_ICONS = {"high": "🔴", "normal": "🟠", "low": "🟡"}
PRIORITY_ORDER = {"high": 0, "normal": 1, "low": 2}
_WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


def today_in(tz_name: str) -> date:
    try:
        return datetime.now(ZoneInfo(tz_name)).date()
    except Exception as exc:  # ZoneInfoNotFoundError, ValueError
        raise ConfigError(f"unknown timezone: {tz_name}") from exc


def parse_priority(value: str) -> str:
    value = PRIORITY_ALIASES.get(value.strip().lower(), value.strip().lower())
    if value not in PRIORITIES:
        raise InvalidRequestError(f"priority must be one of {', '.join(PRIORITIES)}")
    return value


def parse_category(value: str, allowed: tuple[str, ...]) -> str:
    value = value.strip().lower()
    if value not in allowed:
        raise InvalidRequestError(f"category must be one of {', '.join(allowed)}")
    return value


def parse_due(value: str | None, today: date) -> str | None:
    """today|tomorrow|week|none, their French aliases, +Nd, DD/MM[/YYYY] or YYYY-MM-DD -> ISO date or None.

    `week` means the coming Sunday (today if today is Sunday). DD/MM without a year is the next
    such day: this year, or next year if it has already passed.
    """
    if value is None:
        return None
    v = value.strip().lower()
    if v in ("", "none", "aucune"):
        return None
    if v in ("today", "aujourdhui", "aujourd'hui"):
        return today.isoformat()
    if v in ("tomorrow", "demain"):
        return (today + timedelta(days=1)).isoformat()
    if v in ("week", "semaine"):
        return (today + timedelta(days=6 - today.weekday())).isoformat()
    m = re.fullmatch(r"\+(\d{1,3})d", v)
    if m:
        return (today + timedelta(days=int(m.group(1)))).isoformat()
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{4}))?", v)
    try:
        if m:
            day, month = int(m[1]), int(m[2])
            if m[3]:
                return date(int(m[3]), month, day).isoformat()
            due = date(today.year, month, day)
            return (due if due >= today else date(today.year + 1, month, day)).isoformat()
        return date.fromisoformat(v).isoformat()
    except ValueError:
        raise InvalidRequestError(
            f"invalid due date: {value!r} (today, tomorrow, week, none, +Nd, DD/MM[/YYYY] or YYYY-MM-DD)")


def _fmt_day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day:02d}/{d.month:02d}"


def format_line(task: Task) -> str:
    due = f" · {_fmt_day(task.due_date)}" if task.due_date else ""
    return f"{PRIORITY_ICONS.get(task.priority, '')} #{task.id} {task.title}{due}"


def atoi_line(counts: dict[str, int] | None) -> str | None:
    """« 📥 À toi : N … » for the daily message (todo #164); None when nothing waits or the count failed."""
    if not counts:
        return None
    total = sum(counts.values())
    if not total:
        return None
    detail = " · ".join(f"{icon} {n}" for icon, n in counts.items() if n)
    chose = "chose attend" if total == 1 else "choses attendent"
    return f"📥 À toi : {total} {chose} ton ✅ ({detail}) → /start"


def build_digest(open_tasks: list[Task], today: date,
                 atoi_counts: dict[str, int] | None = None) -> dict:
    """Group open tasks for the daily message. Returns counts and the French text.

    Only the 🎯 chantiers are listed; the 🌱 fond tasks are a single count line. `atoi_counts` (icon -> count of
    items waiting for the user) adds the « À toi » line right under the title.
    """
    fond = sum(t.nature == "fond" for t in open_tasks)
    total = len(open_tasks)
    open_tasks = [t for t in open_tasks if t.nature != "fond"]
    iso = today.isoformat()
    overdue = [t for t in open_tasks if t.due_date and t.due_date < iso]
    due_today = [t for t in open_tasks if t.due_date == iso]
    high_undated = [t for t in open_tasks if t.priority == "high" and not t.due_date]

    lines = [f"📋 Todo — {_WEEKDAYS[today.weekday()]} {today.day:02d}/{today.month:02d}"]
    atoi = atoi_line(atoi_counts)
    if atoi:
        lines += ["", atoi]
    for label, group in (("🔥 En retard", overdue), ("📅 Aujourd'hui", due_today),
                         ("⭐ Prioritaires sans échéance", high_undated)):
        if group:
            lines += ["", f"{label} ({len(group)})"] + [f"• {format_line(t)}" for t in group]
    if not (overdue or due_today or high_undated):
        lines += ["", "✅ Rien en retard ni prévu aujourd'hui."]
    lines += ["", f"{total} tâche(s) ouverte(s) au total."]
    if fond:
        lines.append(f"🌱 {fond} tâche{'s' if fond > 1 else ''} de fond")
    return {"overdue": len(overdue), "today": len(due_today), "high_undated": len(high_undated),
            "open": total, "fond": fond, "atoi": sum((atoi_counts or {}).values()), "text": "\n".join(lines)}


def resolve_hermes(binary: str | None) -> str:
    candidate = binary or shutil.which("hermes") or str(Path.home() / ".local/bin/hermes")
    if not Path(candidate).is_file():
        raise ConfigError(f"hermes binary not found: {candidate}")
    return candidate


def send_message(text: str, target: str, binary: str | None, timeout_s: float = 60, sujet: str | None = None,
                 boutons: list | None = None, chantier: str | None = None, thread: int | None = None,
                 cle_alerte: str | None = None, fil_seul: bool = False, genre: str | None = None):
    """Deliver `text` through the common sending point (`envoi`: `hermes send` or the direct Bot API), no LLM involved.
    `boutons` (todo #201): inline buttons in direct mode only (`ultron_hub.boutons` builds them). `genre`: the Fil's
    genre of the notification (`notifs.GENRES`), deduced from the text when absent."""
    from ultron_hub import envoi
    hermes = resolve_hermes(binary) if envoi.mode() == "hermes" else None
    result = envoi.envoyer(text, target=target, binary=hermes, style="stdin", timeout_s=timeout_s, sujet=sujet,
                           boutons=boutons, chantier=chantier, thread=thread, cle_alerte=cle_alerte, fil_seul=fil_seul,
                           **({"genre": genre} if genre else {}))
    if not result.ok:
        raise UltronHubError(result.detail)
    log.info("digest sent to %s", target)
    return result


def board_rank(task: Task, today: date) -> tuple:
    """Order of the Telegram list: overdue, due today, high priority, then the rest (soonest first)."""
    due = task.due_date
    if due and due < today.isoformat():
        group = 0
    elif due == today.isoformat():
        group = 1
    elif task.priority == "high":
        group = 2
    else:
        group = 3
    return (group, due is None, due or "", PRIORITY_ORDER.get(task.priority, 9), task.id)


# ---------------------------------------------------------------------------
# Delivered todos (todo #153): work done without a branch, waiting for the user's ✅
# ---------------------------------------------------------------------------

LIVREE_NOTE_MAX = 200


def parse_livree_note(raw: str | None) -> str:
    note = " ".join((raw or "").split())
    if len(note) > LIVREE_NOTE_MAX:
        raise InvalidRequestError(f"--note must be {LIVREE_NOTE_MAX} characters or fewer (got {len(note)})")
    return note


def is_delivered(events: list[tuple[str, dict]]) -> bool:
    """An open todo counts as delivered while its last event is `livree`.

    Any later event (reopened, edited, nature or chantier changed, closed) cancels it. An `edit` that only touches
    `clair` (filled in by the digest, not by a person) does not.
    """
    for action, details in reversed(events):
        if action == "edit" and set(details) <= {"clair"}:
            continue
        return action == "livree"
    return False
