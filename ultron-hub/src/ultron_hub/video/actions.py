"""`ultron-hub video actions <id>` (todo #93): the user's reply to the report becomes proposed actions.

Since todo #441 the report guesses what the user is looking for (`grille`); his reply may correct that guess in one
sentence (« non, c'était pour … »): the prompt then proposes again for the right intention.

The text of the user (stdin, never argv) is his interpretation and his instruction. Claude (task `video-actions`,
sonnet, no tools) reads the saved analysis (`analyse.load_analysis`) and that text, and proposes at most
MAX_ACTIONS concrete actions of four kinds (the shared logic is in `ultron_hub.proposals`, also used by `docs
actions`): a fiche of the knowledge notebook (`fiche`; a « note » is a fiche), a todo (`todo`), a research
(`recherche`, todo #156: a todo with a research brief), a fact for the profile
(`profil`). NOTHING is done here: every action is only prepared, the plugin shows each one on an approval card and
runs the existing mechanism on its ✅:
    fiche     -> a draft var/video/<id>/actions/<n>.md ; ✅ = signed `savoir ranger` on the draft's fingerprint ;
    todo      -> title (40 characters), notes and « en clair » ready to use ; ✅ = `todo add --source video:<id>` ;
    recherche -> same as a todo (nothing runs by itself) ;
    profil    -> one sentence ; ✅ = `profil propose` (the user still validates it in /profil).
The analysis comes from an untrusted video and the user's text is data too: both sit between tags with `<` `>`
neutralised, and Claude's answers are cleaned like the other video modes. Only what the user's instruction asks for is
proposed (the prompt says so); a vague reply gives no action and a sentence saying what was understood.
Outputs (0600) in var/video/<id>/: `actions.json` (what was proposed) and the fiche drafts.
"""

import json
from typing import Any

from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import Settings
from ultron_hub.proposals import (ACTIONS_DIR, ACTIONS_JSON, FACT_MAX, KINDS, MAX_ACTIONS, NOTES_CHARS,  # noqa: F401
                                  PROFIL_MAX, SAID_CHARS, TITLE_MAX, ActionsError, Source, propose)
from ultron_hub.proposals import build_actions as build_source_actions
from ultron_hub.video import analyse
from ultron_hub.video import collect as collector
from ultron_hub.video.text import neutral

log = get_logger("video")

TASK = "video-actions"
CHRONOLOGY_CHARS = 40_000     # what Claude reads of the timeline (the digest and overview come first)


def analysis_text(analysis: dict[str, Any]) -> str:
    """What Claude reads of the analysis: identity, overview, digest, the guess and its grid (todo #441), then the
    timeline as far as it fits."""
    head = {"title": analysis.get("title"), "author": analysis.get("author"), "duration": analysis.get("duration"),
            "url": analysis.get("url"), "overview": analysis.get("overview"),
            "digest": [f"{collector.timestamp(d.get('time'))} {d.get('text')}" for d in analysis.get("digest") or []
                       if isinstance(d, dict)],
            "not_read": analysis.get("unreadable") or []}
    if isinstance(analysis.get("grille"), dict):  # todo #441: the guess and its grid, to read a correction against
        grid = analyse.grid_view(analysis["grille"])  # todo #443: an older grid read in today's shape
        head["ce_que_je_pense_que_tu_cherches"] = grid["devine"]["phrase"]
        head["cas_devine"] = grid["cas"]
        head["cas_secondaires"] = grid["cas_secondaires"]
        head["grille"] = {k: grid[k] for k in ("inspiration", "info", "methode", "donnees", "ecarts", "a_verifier")}
        head["deja_propose"] = [f"{a.get('type')} : {a.get('titre')}" for a in analysis.get("rangement") or []
                                if isinstance(a, dict)]
    entries = [e for e in analysis.get("timeline") or [] if isinstance(e, dict)]
    return neutral(json.dumps(head, ensure_ascii=False) + "\n\nChronologie :\n"
                   + analyse.chronology(entries, 400)[:CHRONOLOGY_CHARS])


def video_source(video_id: str, analysis: dict[str, Any]) -> Source:
    """Where the proposals point to (also used by the batch of `video analyse`, todo #441)."""
    author = analysis.get("author")
    starts = [e["start"] for e in analysis.get("timeline") or [] if isinstance(e, dict)
              and isinstance(e.get("start"), (int, float))]
    return Source(collector.video_dir(video_id), "video", str(analysis.get("title") or "Vidéo"),
                  f"analyse de la vidéo « {analysis.get('title')} »" + (f" ({author})" if author else ""),
                  "d'une vidéo", analysis.get("url") or "", timed=True, starts=starts, log=log)


def build_actions(settings: Settings, video_id: str, raw_actions: Any, analysis: dict[str, Any],
                  answer: str) -> list[dict[str, Any]]:
    """The proposals of Claude for a video, checked and made ready (drafts written); anything unusable is dropped."""
    return build_source_actions(settings, video_source(video_id, analysis), raw_actions,
                                str(analysis.get("author") or ""), answer)


def do_actions(settings: Settings, video_id: str, raw_answer: str, requested_by: str | None) -> dict[str, Any]:
    """`video actions <id>` (the video lock is held by the caller). Returns an envelope; writes nothing but drafts."""
    def load() -> tuple[Source, str, str]:
        analysis = analyse.load_analysis(video_id)
        return video_source(video_id, analysis), analysis_text(analysis), str(analysis.get("author") or "")

    return propose(settings, task=TASK, command="video.actions", kind="video", source_id=video_id,
                   raw_answer=raw_answer, requested_by=requested_by, load=load)
