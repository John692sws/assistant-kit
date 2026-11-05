"""`ultron-hub video collect | probe | analyse | actions | lot | ranger-lot`: one path for a YouTube or TikTok video (todos #91 to #94).

Each command prints one JSON object on stdout:
    {"status": "ok"|"error", "command": "video.<cmd>", "summary": str, "data": obj|null, "error": str|null}
- `collect <url>`        [--session <name>] [--phrase-stdin] downloads, transcribes and extracts images and sound ONCE (script, no AI):
                         var/video/<id>/ (collected.json, images/); data.id = <id>. `--session` (an ultron session name)
                         and `--phrase-stdin` (the user's optional sentence, 500 characters at most) are kept as
                         collected.json's `intention` (todo #440); the sentence is data, never in the journal or logs.
- `probe <url>`         duration only (yt-dlp, nothing downloaded, no lock): the confirmation of 📥 Analyser (todo #173).
- `analyse <id>`         complete analysis by Claude Opus (video/analyse.py): neutral chronology, then the grid of the guessed
                         intention and its batch of proposals (todo #441, data.actions); report + attachment.
- `analyse-lien <url>`   `collect` then `analyse` in one go, one lock (todo #335); data = the analysis' (id, text, path);
                         if only the analysis fails, data.id is kept to retry with `analyse <id>`.
- `actions <id>` (stdin) the user's reply to the report becomes proposed actions (video/actions.py), none done.
- `lot <id>`, `ranger-lot <id> --empreinte X`  the pending proposals, then all of them filed on ONE signed ✅ (video/lot.py, todo #442).
One video at a time on the server (file lock). The shared journal gets one line per step, never the transcript.
"""

import argparse
import fcntl
import sys
from contextlib import contextmanager
from typing import Any, Iterator

from ultron_hub.common.errors import UltronHubError
from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import Settings
from ultron_hub.core.job import new_job_id
from ultron_hub.video import collect as collector
from ultron_hub.video.links import is_allowed
from ultron_hub.sessions.journal import NAME_RE as SESSION_NAME_RE, chantier_of
from ultron_hub.video import text as vtext
from ultron_hub.video.text import MAX_ANSWER_CHARS

log = get_logger("video")

PHRASE_MAX_CHARS = 500
BUSY_TEXT = "Une vidéo est déjà en cours de traitement. Réessaie dans quelques minutes."


def envelope(status: str = "ok", summary: str = "", data: Any = None, error: str | None = None,
             command: str = "video.collect") -> dict[str, Any]:
    return {"status": status, "command": command, "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    video = sub.add_parser("video", help="collect, analyse or turn into actions a YouTube or TikTok video")
    cmds = video.add_subparsers(dest="video_command", required=True, parser_class=parser_class)
    coll = cmds.add_parser("collect", help="download, transcribe and extract images and sound (data.id for `video analyse`)")
    coll.add_argument("url")
    coll.add_argument("--requested-by", default=None, help="e.g. telegram:<chat_id>")
    coll.add_argument("--session", default=None, help="the ultron session the video is sent from (its chantier is kept)")
    coll.add_argument("--phrase-stdin", action="store_true", help="read the user's optional sentence from stdin (500 characters at most)")
    analyse = cmds.add_parser("analyse", help="analysis of a collected video by the guessed intention (todos #92, #441)")
    analyse.add_argument("id")
    analyse.add_argument("--requested-by", default=None, help="e.g. telegram:<chat_id>")
    both = cmds.add_parser("analyse-lien", help="collect then analyse a link in one go (todo #335)")
    both.add_argument("url")
    both.add_argument("--requested-by", default=None, help="e.g. telegram:<chat_id>")
    both.add_argument("--session", default=None, help="the ultron session the video is sent from (its chantier is kept)")
    both.add_argument("--phrase-stdin", action="store_true", help="read the user's optional sentence from stdin (500 characters at most)")
    probe = cmds.add_parser("probe", help="duration of a video before any download (todo #173); data.duration is null if unreadable")
    probe.add_argument("url")
    actions = cmds.add_parser("actions", help="the user's reply (stdin) to a report, or a correction of its guess, becomes proposed actions (todos #93, #441)")
    actions.add_argument("id")
    actions.add_argument("--requested-by", default=None, help="e.g. telegram:<chat_id>")
    lot = cmds.add_parser("lot", help="the proposals of a video still pending and their fingerprint (read only, todo #442)")
    lot.add_argument("id")
    ranger = cmds.add_parser("ranger-lot", help="file every pending proposal of a video (signed video-lot on <id>:<fingerprint>)")
    ranger.add_argument("id")
    ranger.add_argument("--empreinte", required=True, help="fingerprint shown by `video lot <id>`")
    ranger.add_argument("--i-am-human", dest="human", action="store_true",
                        help="confirm at a real terminal (typed code) instead of a signed button")


class Busy(Exception):
    pass


@contextmanager
def single_video(settings: Settings) -> Iterator[None]:
    """Non-blocking exclusive lock: a second video while one runs is refused, not queued."""
    settings.var_dir.joinpath("video").mkdir(parents=True, exist_ok=True)
    with open(settings.var_dir / "video" / ".lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy from None
        yield


def valid_id(video_id: str) -> bool:
    return isinstance(video_id, str) and collector.VIDEO_ID_RE.fullmatch(video_id) is not None


def read_phrase(raw: str) -> str | None:
    """The user's optional sentence: cleaned like a reply (text.clean_answer), empty = none, refused beyond 500 characters."""
    try:
        text = vtext.clean_answer(raw)
    except vtext.ReplyError:
        if not raw.strip():
            return None
        text = ""
    if not text or len(text) > PHRASE_MAX_CHARS:
        raise UltronHubError(f"Phrase trop longue : {PHRASE_MAX_CHARS} caractères au plus.")
    return text


def intention_of(session: str | None, phrase: str | None) -> dict[str, str | None]:
    return {"session": session, "chantier": chantier_of(session) if session else None, "phrase": phrase}


def do_collect(settings: Settings, url: str, intention: dict[str, str | None] | None = None) -> dict[str, Any]:
    """Download + transcribe + images and sound (lock held by the caller); keeps var/video/<id>/collected.json."""
    video_id = new_job_id()
    data = collector.collect_video(url, video_id)
    data["intention"] = intention or intention_of(None, None)
    collector.save_collected(video_id, data)
    return {"id": video_id, "title": data.get("title"), "author": data.get("author"), "duration": data.get("duration")}


def handle(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    cmd = args.video_command
    command = f"video.{cmd}"
    if cmd in ("collect", "analyse-lien"):
        if not is_allowed(args.url):
            return envelope(status="error", summary="lien refusé", command=command,
                            error="Lien refusé : seuls les liens https YouTube et TikTok sont acceptés.")
    elif cmd == "probe":
        if not is_allowed(args.url):
            return envelope(status="error", summary="lien refusé", command=command,
                            error="Lien refusé : seuls les liens https YouTube et TikTok sont acceptés.")
        duration = collector.probe_duration(args.url)  # read only, no lock: the video lock stays for collect/analyse
        return envelope(summary="durée lue" if duration is not None else "durée inconnue",
                        data={"duration": duration}, command=command)
    elif not valid_id(args.id):
        return envelope(status="error", summary="identifiant invalide", command=command,
                        error="Identifiant de vidéo invalide.")
    if cmd in ("lot", "ranger-lot"):  # its own lock per video (lot.py): never waits for an analysis
        from ultron_hub.video import lot
        return lot.handle(args, settings, envelope)
    intention = None
    if cmd in ("collect", "analyse-lien"):
        session = getattr(args, "session", None)
        if session is not None and not SESSION_NAME_RE.fullmatch(session):
            return envelope(status="error", summary="session refusée", command=command,
                            error="Nom de session refusé.")
        try:
            phrase = read_phrase(sys.stdin.read(PHRASE_MAX_CHARS * 4 + 1)) if getattr(args, "phrase_stdin", False) else None
        except UltronHubError as exc:
            return envelope(status="error", summary="phrase refusée", command=command, error=str(exc))
        intention = intention_of(session, phrase)
    try:
        with single_video(settings):
            if cmd == "analyse":
                from ultron_hub.video import analyse  # imported here: analyse itself imports this module
                return analyse.do_analyse(settings, args.id, args.requested_by)
            if cmd == "actions":
                from ultron_hub.video import actions  # same reason
                # stdin, never argv: the reply is the user's free text.
                return actions.do_actions(settings, args.id, sys.stdin.read(MAX_ANSWER_CHARS * 4 + 1),
                                          args.requested_by)
            try:
                collected = do_collect(settings, args.url, intention)
            except UltronHubError as exc:
                log.warning("video collect failed requested_by=%s: %s", args.requested_by, exc)
                return envelope(status="error", summary="téléchargement impossible", error=str(exc), command=command)
            log.info("video=%s collected requested_by=%s session=%s phrase_chars=%d", collected["id"], args.requested_by,
                     intention["session"], len(intention["phrase"] or ""))
            if cmd == "analyse-lien":
                from ultron_hub.video import analyse
                done = analyse.do_analyse(settings, collected["id"], args.requested_by)
                done["command"] = command
                if done.get("status") != "ok":  # keep the id: the collection is not lost, `video analyse <id>` retries
                    done["data"] = {**(done.get("data") or {}), "id": collected["id"]}
                return done
            return envelope(summary=f"Vidéo prête : {collected['title'] or args.url}", data=collected,
                            command=command)
    except Busy:
        return envelope(status="error", summary="déjà en cours", error=BUSY_TEXT, data={"busy": True},
                        command=command)
