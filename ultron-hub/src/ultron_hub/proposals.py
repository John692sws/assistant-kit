"""Proposed actions shared by `video actions` (todo #93) and `docs actions` (todo #156).

The user's reply to a neutral analysis (stdin, never argv) is turned by Claude into at most MAX_ACTIONS concrete
proposals of four kinds: a fiche of the knowledge notebook (`fiche`), a todo (`todo`), a research (`recherche`: a todo
with a research brief; nothing runs by itself), a fact for the profile (`profil`). NOTHING is done here: every
action is only prepared, the plugin shows each one on an approval card and runs the existing mechanism on its ✅.
A `Source` says where the analysis comes from (a video or a batch of documents); the rest is the same.
The analysis and the user's text are data: both sit between tags with `<` `>` neutralised.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ultron_hub.common.errors import UltronHubError
from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import Settings
from ultron_hub.core.job import JobRequest, new_job_id
from ultron_hub.core.runner import run_job
from ultron_hub.esprit import savoir
from ultron_hub.esprit import store as esprit
from ultron_hub.video import collect as collector
from ultron_hub.video import text
from ultron_hub.video.text import neutral

MAX_ACTIONS = 5
PROFIL_MAX = 1                # one fact at a time: the user validates it in /profil
NOTES_CHARS = 3800            # under the todo notes limit (4000)
SAID_CHARS = 400              # the user's own words kept in a fiche or in the notes of a todo
ACTIONS_JSON = "actions.json"
ACTIONS_DIR = "actions"
KINDS = ("fiche", "todo", "recherche", "profil")
TITLE_MAX = 40
FACT_MAX = 300
RESEARCH_NOTES = ("Type : recherche web (profil Chercheur). Livrable : un rapport Markdown en français, avec les sources "
                  "citées. Aucun changement de code.")


class ActionsError(UltronHubError):
    """A failure explained to the user in one sentence."""


@dataclass
class Source:
    """What the proposals are about. `folder` holds the outputs; `name` is « video » or « docs » (log label);
    `describe` goes in the notes (« analyse de la vidéo « T » (A) »); `journal` ends « Actions proposées après
    l'analyse … » in the shared journal; `url` is the link of a fiche (a video link, or
    `docs:<id>`); `timed` = the link can carry a moment; `starts` = seconds of a timeline an action may point to."""
    folder: Path
    name: str
    title: str
    describe: str
    journal: str
    url: str
    timed: bool = False
    starts: list[float] = field(default_factory=list)
    log: Any = None

    def link(self, at: int | None) -> str:
        return text.timed_link(self.url, at) if self.timed else self.url


def _snap(raw: Any, source: Source) -> int | None:
    """The second of the nearest timeline entry (never an invented moment); None when the action has no moment."""
    if not isinstance(raw, (int, float)) or isinstance(raw, bool) or raw <= 0 or not source.starts:
        return None
    return int(min(source.starts, key=lambda s: abs(s - raw)))


def _said(answer: str) -> str:
    return text.cut_prose(" ".join(answer.split()), SAID_CHARS)


def _todo_notes(raw: dict[str, Any], source: Source, answer: str, link: str, at: int | None, kind: str) -> str:
    """Notes of a todo that stand on their own: what and why (Claude), the user's words, the source. A research
    also says what kind of work it is."""
    lines = [text.clean_text(raw.get("corps"), 1500) or text.clean_text(raw.get("resume"), 200), ""]
    if answer.strip():  # the batch of an analysis (todo #441) may have no words of the user
        lines += [f"Ce que tu en as dit : « {_said(answer)} »", ""]
    if kind == "recherche":
        lines += [RESEARCH_NOTES, ""]
    lines.append(f"Source : {source.describe}" + (f", passage à {collector.timestamp(at)}" if at is not None else ""))
    if link:
        lines.append(f"Lien : {link}")
    notes = "\n".join(lines)
    return notes if len(notes) <= NOTES_CHARS else notes[:NOTES_CHARS - 1].rstrip() + "…"


def _fiche_draft(settings: Settings, source: Source, number: int, raw: dict[str, Any], author: str, answer: str,
                 link: str) -> dict[str, Any] | None:
    """The draft of a fiche through savoir.write_draft, shortened until it fits 3 Kio. None = no usable fiche."""
    title = text.cut_words(text.clean_text(raw.get("titre"), 300), savoir.TITLE_MAX)
    retenir = text.cut_prose(text.clean_text(raw.get("resume"), 300), savoir.RETENIR_MAX)
    detail = text.clean_text(raw.get("corps"), 1500)
    if not title or not (retenir or detail):
        return None
    projet = raw.get("projet") if raw.get("projet") in savoir.PROJETS else "autre"
    fields = {"titre": title, "source": link, "auteur": text.clean_text(author, 100),
              "but": "retenir", "projet": projet, "tags": text.clean_tags(raw.get("tags")), "todos": []}
    path = source.folder / ACTIONS_DIR / f"{number}.md"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    for size, said in ((1500, True), (900, True), (450, False), (200, False)):
        body = ["## À retenir", retenir or text.cut_prose(detail, savoir.RETENIR_MAX)]
        if detail:
            body += ["", "## Détail", text.cut_prose(detail, size)]
        if said and answer.strip():
            body += ["", "## Ce que tu en as dit", _said(answer)]
        try:
            draft = savoir.write_draft(path, fields, "\n".join(body), settings)
            return {"path": str(path), "empreinte": draft["empreinte"], "removed": draft["removed"],
                    "apercu": draft["text"]}
        except savoir.SavoirError as exc:
            if "fiche trop longue" not in str(exc):
                source.log.info("%s action %d: no fiche (%s)", source.name, number, exc)
                return None
    return None


def build_actions(settings: Settings, source: Source, raw_actions: Any, author: str, answer: str,
                  chantiers: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """The proposals of Claude, checked and made ready (drafts written); anything unusable is dropped. A todo whose
    `chantier` is one of `chantiers` (the known names) carries it: its ✅ files it there (`todo add --chantier`)."""
    out: list[dict[str, Any]] = []
    profils = 0
    for raw in raw_actions if isinstance(raw_actions, list) else []:
        if len(out) >= MAX_ACTIONS:
            break
        if not isinstance(raw, dict) or raw.get("type") not in KINDS:
            continue
        kind, number = raw["type"], len(out) + 1
        at = _snap(raw.get("time"), source)
        link = source.link(at)
        why = text.clean_text(raw.get("resume"), 200)
        if kind == "fiche":
            draft = _fiche_draft(settings, source, number, raw, author, answer, link)
            if draft is None:
                continue
            fields = savoir.parse(draft["apercu"])[0]
            out.append({"n": number, "type": kind, "titre": fields["titre"], "resume": why, **draft})
        elif kind in ("todo", "recherche"):
            title = text.cut_title(text.clean_text(raw.get("titre"), 300), TITLE_MAX)
            if not title:
                continue
            projet = raw.get("projet") if raw.get("projet") in savoir.PROJETS else None
            clair = text.clair_line(text.clean_text(raw.get("clair"), 300) or why or title)
            item = {"n": number, "type": kind, "titre": title, "resume": why, "clair": clair,
                    "notes": _todo_notes(raw, source, answer, link, at, kind),
                    "categorie": text.todo_category(projet, settings.todo_categories)}
            if isinstance(raw.get("chantier"), str) and raw["chantier"] in chantiers:
                item["chantier"] = raw["chantier"]
            out.append(item)
        else:
            fact = text.cut_words(text.clean_text(raw.get("corps"), 400), FACT_MAX)
            if not fact or profils >= PROFIL_MAX:
                continue
            profils += 1
            out.append({"n": number, "type": kind, "titre": "Fait sur toi", "resume": why, "fait": fact})
    return out


def write_private(path: Path, content: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)


def propose(settings: Settings, *, task: str, command: str, kind: str, source_id: str, raw_answer: str,
            requested_by: str | None, load: Callable[[], tuple[Source, str, str]]) -> dict[str, Any]:
    """The whole `<kind> actions <id>` command. `load()` returns (source, analysis text for the prompt, author) and
    may raise an UltronHubError (explained to the user). Returns an envelope; writes nothing but drafts and
    actions.json."""
    log = get_logger(kind)

    def envelope(status: str = "ok", summary: str = "", data: Any = None, error: str | None = None) -> dict[str, Any]:
        return {"status": status, "command": command, "summary": summary, "data": data, "error": error}

    def refused(error: str) -> dict[str, Any]:
        return envelope(status="error", summary="propositions impossibles", error=error)

    try:
        answer = text.clean_answer(raw_answer)
        source, analysis, author = load()
        source.log = log
        profil = esprit.persona(settings).get("profil") or "(profil indisponible)"
        params = {"analyse": analysis, "profil": profil, "reponse": neutral(answer)}
        request = JobRequest.from_dict({"job_id": new_job_id(), "action": task, "params": params,
                                        "requested_by": requested_by})
        result = run_job(request, settings)
        if not result.ok or not isinstance(result.data, dict):
            return refused(result.error or result.summary or "Je n'ai pas réussi à préparer des actions.")
        actions = build_actions(settings, source, result.data.get("actions"), author, answer)
    except UltronHubError as exc:
        return refused(str(exc))
    said = text.clean_text(result.data.get("reponse"), 400)
    write_private(source.folder / ACTIONS_JSON, json.dumps({"reponse": said, "actions": actions}, ensure_ascii=False))
    esprit.record(settings, "ultron-hub", kind,
                  f"Actions proposées après l'analyse {source.journal} : {source.title} ({len(actions)})")
    log.info("%s=%s actions proposed: %d (%s)", kind, source_id, len(actions), ",".join(a["type"] for a in actions))
    return envelope(summary=f"{len(actions)} action(s) proposée(s)",
                    data={"id": source_id, "title": source.title, "reponse": said, "actions": actions,
                          "cost_usd": result.cost_usd})
