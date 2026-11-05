"""`ultron-hub montage prepare | render | make`: a voice becomes a vertical animated MP4 (todo #73).

Each command prints one JSON object on stdout:
    {"status": "ok"|"error", "command": "montage.<cmd>", "summary": str, "data": obj|null, "error": str|null}
- `prepare|make ... --style noir|canevas|papier` (todo #434) chooses the style BEFORE the montage (default noir).
- `prepare|make ... --fin abonne|suite|rien` (todo #438) chooses the end of the video (default abonne).
- `prepare <audio>`  voice -> mono WAV, long silences cut (ffmpeg), words with timestamps (Whisper of the
                     hermes-video skill), animation plan (task montage-plan, closed catalogue); data.id + preview.
- `render <id> [--apply E]`  the current plan -> var/montage/<id>/v<n>.mp4 (next version, Remotion), checked by
                     ffprobe; with --apply, first the pending retouch whose fingerprint is E (todo #75).
- `make <audio>`     prepare + render in one go.
- `prepare|make --recu <name>` (todo #437) the audio or video is a file of the dashboard's 📎 joindre (var/recus): only its
                     stored name is given, resolved by `recus.chemin` (no free path), audio and video only.
- `prepare|make --script`  (todo #435) no audio: the script is read on stdin (never argv) and spoken by the local
                     voice Piper (tts.py); the true text is then set on the timestamps heard by Whisper.
- `fix <id>`         ✏️ Corriger le texte (todo #74): the corrected text on stdin, set on the voice's timestamps,
                     then a new plan (same preview as `prepare`).
- `keep <id>`        📚 Garder: the montage is marked as kept (never purged).
- `photos <id> <file>...`  🖼 (todo #76) 1 to 5 photos of the user (captions as a JSON list on stdin) checked and
                     converted to JPEG, then a new plan that places them (same preview as `prepare`).
- `retouch <id> --scene N | --style THEME | --duree CHOICE`  (todo #75) proposes ONE retouch (pending.json) and
                     returns its preview and fingerprint; nothing changes before `render --apply`.
- `mesure <id> [--version N]` (todo #436) read-only: frozen run and filling of the scenes band (render.measure).
- `lire`             (todo #439) text on stdin spoken by the same voice -> data.nom/url (lecture.py), for the dashboard.
- `purge [--days N]` removes the montages older than N days (30) that were not kept; also run by `prepare`.
One montage at a time (var/montage/.lock). The shared journal gets the title only, never the words.
"""

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ultron_hub.common.errors import UltronHubError
from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import Settings
from ultron_hub import recus
from ultron_hub.core.job import JobRequest, new_job_id
from ultron_hub.core.runner import run_job
from ultron_hub.esprit import store as esprit
from ultron_hub.montage import audio, lecture, photos, plan, render, retouch, stock, store, transcribe, tts

log = get_logger("montage")
TASK = "montage-plan"
SCENE_TASK = "montage-scene"


def envelope(status: str = "ok", summary: str = "", data: Any = None, error: str | None = None,
             command: str = "montage") -> dict[str, Any]:
    return {"status": status, "command": command, "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    montage = sub.add_parser("montage", help="turn a voice into a vertical animated video (Remotion)")
    cmds = montage.add_subparsers(dest="montage_command", required=True, parser_class=parser_class)
    prepare = cmds.add_parser("prepare", help="clean and transcribe a voice, plan the animations (data.id)")
    prepare.add_argument("audio", nargs="?", default=None)
    prepare.add_argument("--script", action="store_true", help="text on stdin read by the synthetic voice")
    prepare.add_argument("--recu", default=None, help="name of a file received by 📎 joindre (audio or video), not a path")
    prepare.add_argument("--style", choices=plan.THEMES, default=plan.DEFAULT_THEME,
                         help="video style (a whole universe), written in the plan")
    prepare.add_argument("--fin", choices=plan.FINS, default=plan.DEFAULT_FIN,
                         help="end of the video: subscribe card, next-video card or a plain fade, written in the plan")
    prepare.add_argument("--requested-by", default=None, help="e.g. telegram:<chat_id>")
    rend = cmds.add_parser("render", help="render a prepared montage to MP4")
    rend.add_argument("id")
    rend.add_argument("--apply", default=None, help="fingerprint of the pending retouch to apply first")
    rend.add_argument("--requested-by", default=None, help="e.g. telegram:<chat_id>")
    make = cmds.add_parser("make", help="prepare + render in one go")
    make.add_argument("audio", nargs="?", default=None)
    make.add_argument("--script", action="store_true", help="text on stdin read by the synthetic voice")
    make.add_argument("--recu", default=None, help="name of a file received by 📎 joindre (audio or video), not a path")
    make.add_argument("--style", choices=plan.THEMES, default=plan.DEFAULT_THEME,
                      help="video style (a whole universe), written in the plan")
    make.add_argument("--fin", choices=plan.FINS, default=plan.DEFAULT_FIN,
                      help="end of the video: subscribe card, next-video card or a plain fade, written in the plan")
    make.add_argument("--requested-by", default=None, help="e.g. telegram:<chat_id>")
    fix = cmds.add_parser("fix", help="corrected text on stdin -> words realigned, new plan")
    fix.add_argument("id")
    fix.add_argument("--requested-by", default=None, help="e.g. telegram:<chat_id>")
    pho = cmds.add_parser("photos", help="1 to 5 photos (captions as a JSON list on stdin) -> new plan")
    pho.add_argument("id")
    pho.add_argument("files", nargs="+")
    pho.add_argument("--requested-by", default=None, help="e.g. telegram:<chat_id>")
    keep = cmds.add_parser("keep", help="mark a rendered montage as kept (never purged)")
    keep.add_argument("id")
    ret = cmds.add_parser("retouch", help="propose one retouch (applied by render --apply)")
    ret.add_argument("id")
    which = ret.add_mutually_exclusive_group(required=True)
    which.add_argument("--scene", type=int, help="number of the scene to redo (1 = first)")
    which.add_argument("--style", choices=plan.THEMES)
    which.add_argument("--duree", choices=tuple(retouch.DUREE_CHOICES))
    ret.add_argument("--requested-by", default=None, help="e.g. telegram:<chat_id>")
    mes = cmds.add_parser("mesure", help="read-only: frozen run and filling of the scenes band of a rendered video")
    mes.add_argument("id")
    mes.add_argument("--version", type=int, default=None, help="version number (default: the last one)")
    cmds.add_parser("lire", help="text on stdin spoken by the montage voice -> data.url (todo #439)")
    purge = cmds.add_parser("purge", help="remove montages older than N days that were not kept")
    purge.add_argument("--days", type=int, default=retouch.PURGE_DAYS)


def preview(plan_data: dict[str, Any], words: list[dict[str, Any]]) -> dict[str, Any]:
    """What the user checks before ✅: the cleaned text and one line per scene."""
    return {"title": plan_data["title"], "text": plan.words_text(words),
            "scenes": [plan.scene_line(s) for s in plan_data["scenes"]], "duration_s": plan_data["duration"],
            "theme": plan_data["theme"], "theme_label": plan.THEME_LABELS.get(plan_data["theme"]),
            "fin": plan_data["fin"], "fin_label": plan.FIN_LABELS.get(plan_data["fin"]),
            "photos": sum(1 for s in plan_data["scenes"] if s["type"] == "image")}


def do_prepare(settings: Settings, source: str | None, requested_by: str | None,
               script: str | None = None, style: str = plan.DEFAULT_THEME,
               fin: str = plan.DEFAULT_FIN) -> dict[str, Any]:
    """Lock held by the caller. Returns the envelope of `montage prepare` (`script`: text to synthesize, no audio)."""
    command = "montage.prepare"
    retouch.purge(settings)  # history: old montages that were not kept go (best effort)
    try:
        if script is not None:
            text = tts.check_text(script)
            montage_id = new_job_id()
            folder = store.create(settings, montage_id)
            tts.synthesize(text, folder / "tts.wav")
            durations = audio.clean_voice(folder / "tts.wav", folder)
        else:
            src = audio.check_input(Path(source or ""))
            montage_id = new_job_id()
            folder = store.create(settings, montage_id)
            durations = audio.clean_voice(src, folder)
        heard = transcribe.transcribe(settings, folder / store.VOICE, durations["clean_s"])
        if script is not None:
            try:  # the true text corrects what Whisper heard; if it cannot be set, Whisper's words stay
                heard = {**heard, "words": plan.realign(heard["words"], text)}
            except UltronHubError:
                log.warning("montage=%s script not realigned: Whisper words kept", montage_id)
        store.write(folder / store.WORDS, {"duration": durations["clean_s"], **heard})
    except UltronHubError as exc:
        return envelope(status="error", summary="script refusé" if script is not None else "audio refusé",
                        error=str(exc), command=command)
    out = plan_montage(settings, montage_id, heard["words"], durations["clean_s"], requested_by, command, style,
                       fin)
    if out["status"] == "ok":
        log.info("montage=%s prepared: %d words, %.1f s (%.1f s of silence cut)", montage_id, len(heard["words"]),
                 durations["clean_s"], durations["removed_s"])
        out["data"].update(removed_s=durations["removed_s"], backend=heard["backend"])
        if script is not None:
            out["data"].update(source="script", voice=tts.VOICE_NAME)
        else:
            out["data"].update(source="voix")
    return out


def plan_montage(settings: Settings, montage_id: str, words: list[dict[str, Any]], duration: float,
                 requested_by: str | None, command: str, style: str = plan.DEFAULT_THEME,
                 fin: str = plan.DEFAULT_FIN) -> dict[str, Any]:
    """Task montage-plan on the words of words.json, checked plan written to plan.json; envelope with the preview.
    A previous video of this montage no longer matches: it is removed."""
    folder = store.folder(settings, montage_id)
    request = JobRequest.from_dict({"job_id": new_job_id(), "action": TASK, "params": {"montage_id": montage_id},
                                    "requested_by": requested_by})
    result = run_job(request, settings)
    if not result.ok:
        return envelope(status="error", summary="plan impossible", command=command, data={"id": montage_id},
                        error=result.error or result.summary or "Le plan d'animation a échoué.")
    try:
        checked = stock.resolve(folder, plan.build_plan(result.data, words, duration, theme=style,
                                                           photos=photos.read(folder), fin=fin))
    except UltronHubError as exc:
        return envelope(status="error", summary="plan impossible", error=str(exc), command=command,
                        data={"id": montage_id})
    store.write(folder / store.PLAN, checked)
    (folder / retouch.PENDING).unlink(missing_ok=True)
    return envelope(summary=f"Montage prêt : {checked['title']}", command=command,
                    data={"id": montage_id, **preview(checked, words), "words": len(words),
                          "cost_usd": result.cost_usd})


def current_style(settings: Settings, montage_id: str) -> str:
    """The style already chosen for this montage (a new plan keeps it), the default if there is no readable plan."""
    try:
        return plan.normalized(store.read(settings, montage_id, store.PLAN))["theme"]
    except (UltronHubError, KeyError, TypeError, AttributeError):
        return plan.DEFAULT_THEME


def current_fin(settings: Settings, montage_id: str) -> str:
    """The end already chosen for this montage (a new plan keeps it); an old plan without one ends with `rien`."""
    try:
        return plan.normalized(store.read(settings, montage_id, store.PLAN))["fin"]
    except (UltronHubError, KeyError, TypeError, AttributeError):
        return plan.LEGACY_FIN


def do_fix(settings: Settings, montage_id: str, text: str, requested_by: str | None) -> dict[str, Any]:
    """✏️ Corriger le texte: the user's text replaces the transcription (same timestamps), then a new plan."""
    command = "montage.fix"
    try:
        heard = store.read(settings, montage_id, store.WORDS)
        words = plan.realign(heard["words"], text)
        store.write(store.folder(settings, montage_id) / store.WORDS, {**heard, "words": words, "corrected": True})
    except (UltronHubError, KeyError, TypeError) as exc:
        message = str(exc) if isinstance(exc, UltronHubError) else "Les données de ce montage sont illisibles."
        return envelope(status="error", summary="correction refusée", error=message, command=command,
                        data={"id": montage_id})
    log.info("montage=%s text corrected (%d words)", montage_id, len(words))
    return plan_montage(settings, montage_id, words, float(heard.get("duration") or 0), requested_by, command,
                        current_style(settings, montage_id), current_fin(settings, montage_id))


def do_photos(settings: Settings, montage_id: str, files: list[str], raw_captions: str,
              requested_by: str | None) -> dict[str, Any]:
    """🖼 Ajouter des photos: the user's photos replace the previous ones, then a new plan places them."""
    command = "montage.photos"
    try:
        heard = store.read(settings, montage_id, store.WORDS)
        folder = store.folder(settings, montage_id)
        added = photos.import_photos(folder, files, photos.captions(raw_captions, len(files)))
    except (UltronHubError, KeyError, TypeError) as exc:
        message = str(exc) if isinstance(exc, UltronHubError) else "Les données de ce montage sont illisibles."
        return envelope(status="error", summary="photos refusées", error=message, command=command,
                        data={"id": montage_id})
    log.info("montage=%s %d photo(s) added", montage_id, len(added))
    return plan_montage(settings, montage_id, heard["words"], float(heard.get("duration") or 0), requested_by,
                        command, current_style(settings, montage_id), current_fin(settings, montage_id))


def do_keep(settings: Settings, montage_id: str) -> dict[str, Any]:
    """📚 Garder: the rendered montage is marked as kept (meta.json) and never purged."""
    command = "montage.keep"
    try:
        folder = store.folder(settings, montage_id)
        plan_data = store.read(settings, montage_id, store.PLAN)
        if not store.versions(folder):
            raise store.MontageError("Ce montage n'a pas encore de vidéo : lance d'abord le rendu.")
        meta = store.read_meta(settings, montage_id)
        if meta.get("kept"):
            return envelope(summary="déjà gardé", command=command, data={"id": montage_id, "kept": True,
                                                                          "already": True})
        store.write(folder / store.META, {**meta, "kept": True,
                                          "kept_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    except UltronHubError as exc:
        return envelope(status="error", summary="impossible", error=str(exc), command=command)
    esprit.record(settings, "ultron-hub", "montage", f"Montage vidéo gardé : {plan_data.get('title') or montage_id}")
    return envelope(summary="montage gardé", command=command,
                    data={"id": montage_id, "kept": True, "already": False,
                          "versions": store.versions(folder)})


def do_render(settings: Settings, montage_id: str, requested_by: str | None, apply: str | None = None) -> dict[str, Any]:
    """Lock held by the caller. Renders the next version (v<n>.mp4), after applying the approved retouch if any."""
    command = "montage.render"
    try:
        folder = store.folder(settings, montage_id)
        if apply is not None:
            retouch.apply_pending(folder, retouch.take_pending(folder, apply))
        plan_data = plan.normalized(store.read(settings, montage_id, store.PLAN))
        words = store.read(settings, montage_id, store.WORDS)["words"]
        version = (store.versions(folder) or [0])[-1] + 1
        out = store.video_path(folder, version)
        done = render.render(settings, folder, plan.props(plan_data, words), out,
                             plan.total_duration(plan_data))
    except (UltronHubError, KeyError, TypeError) as exc:
        message = str(exc) if isinstance(exc, UltronHubError) else "Les données de ce montage sont illisibles."
        return envelope(status="error", summary="rendu impossible", error=message, command=command,
                        data={"id": montage_id})
    esprit.record(settings, "ultron-hub", "montage", f"Montage vidéo rendu : {plan_data['title']}")
    log.info("montage=%s rendered in %.1f s (%d bytes) requested_by=%s", montage_id, done["render_s"], done["bytes"],
             requested_by)
    return envelope(summary=f"Vidéo prête : {plan_data['title']}", command=command,
                    data={"id": montage_id, "path": str(out), "version": version, **preview(plan_data, words),
                          **done})


def do_retouch(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    """Lock held by the caller. Writes ONE proposal to pending.json; returns its preview and fingerprint."""
    command = "montage.retouch"
    montage_id = args.id
    try:
        folder = store.folder(settings, montage_id)
        current = plan.normalized(store.read(settings, montage_id, store.PLAN))
        heard = store.read(settings, montage_id, store.WORDS)
        if not store.versions(folder):
            raise store.MontageError("Rends d'abord la vidéo avant de la retoucher.")
        (folder / retouch.NEXT_VOICE).unlink(missing_ok=True)
        cost = None
        if args.style is not None:
            kind, new_plan, words = "style", retouch.with_theme(current, args.style), heard["words"]
            what = f"style « {plan.THEME_LABELS[args.style]} » au lieu de « {plan.THEME_LABELS[current['theme']]} »"
            pending = {"kind": kind, "plan": new_plan}
        elif args.duree is not None:
            new_plan, new_words, cut = retouch.retime(current, heard, args.duree)
            retouch.cut_voice(folder, cut)
            kind, words = "duree", new_words["words"]
            what = (f"{retouch.DUREE_LABELS[args.duree]} : {current['duration']:.0f} s → {new_plan['duration']:.0f} s"
                    .replace(".", ","))
            pending = {"kind": kind, "plan": new_plan, "words": new_words}
        else:
            n = args.scene - 1
            if not 0 <= n < len(current["scenes"]):
                raise store.MontageError("Cette scène n'existe pas.")
            request = JobRequest.from_dict({"job_id": new_job_id(), "action": SCENE_TASK,
                                            "params": {"montage_id": montage_id, "scene": n},
                                            "requested_by": args.requested_by})
            result = run_job(request, settings)
            if not result.ok:
                raise store.MontageError(result.error or "La nouvelle proposition a échoué.")
            cost = result.cost_usd
            new_plan = stock.resolve(folder, retouch.replace_scene(current, heard["words"], n, result.data,
                                                                    photos.read(folder)))
            kind, words = "scene", heard["words"]
            before = plan.scene_line(current["scenes"][n])
            after = plan.scene_line(new_plan["scenes"][n])
            what = f"scène {args.scene} : {before}\n→ {after}"
            pending = {"kind": kind, "n": n, "plan": new_plan}
        fp = retouch.write_pending(folder, pending)
    except (UltronHubError, KeyError, TypeError) as exc:
        message = str(exc) if isinstance(exc, UltronHubError) else "Les données de ce montage sont illisibles."
        return envelope(status="error", summary="retouche impossible", error=message, command=command,
                        data={"id": montage_id})
    log.info("montage=%s retouch %s proposed (%s)", montage_id, kind, fp)
    return envelope(summary="retouche proposée", command=command,
                    data={"id": montage_id, "kind": kind, "empreinte": fp, "what": what,
                          "next_version": store.versions(folder)[-1] + 1, **preview(new_plan, words),
                          "cost_usd": cost})


def do_mesure(settings: Settings, montage_id: str, version: int | None) -> dict[str, Any]:
    """Todo #436: how frozen and how full is the band of the scenes in a rendered version (nothing changes)."""
    command = "montage.mesure"
    try:
        folder = store.folder(settings, montage_id)
        theme = plan.normalized(store.read(settings, montage_id, store.PLAN))["theme"]
        found = store.versions(folder)
        if not found or (version is not None and version not in found):
            raise store.MontageError("Cette version n'existe pas : rends d'abord la vidéo.")
        number = version if version is not None else found[-1]
        result = render.measure(store.video_path(folder, number), theme)
    except (UltronHubError, KeyError, TypeError) as exc:
        message = str(exc) if isinstance(exc, UltronHubError) else "Les données de ce montage sont illisibles."
        return envelope(status="error", summary="mesure impossible", error=message, command=command,
                        data={"id": montage_id})
    return envelope(summary=f"v{number} : figé au plus {result['fige_max_s']} s, remplissage {result['remplissage']}",
                    command=command, data={"id": montage_id, "version": number, **result})


def handle(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    cmd = args.montage_command
    command = f"montage.{cmd}"
    if cmd == "purge":
        return envelope(summary="purge faite", command=command,
                        data={"removed": retouch.purge(settings, max(1, args.days))})
    if cmd in ("render", "fix", "keep", "retouch", "photos", "mesure") and not store.valid_id(args.id):
        return envelope(status="error", summary="identifiant invalide", error="Identifiant de montage invalide.",
                        command=command)
    if cmd in ("prepare", "make") and bool(args.audio) + bool(args.script) + (args.recu is not None) != 1:
        return envelope(status="error", summary="source invalide", command=command,
                        error="Donne soit un fichier audio, soit --recu (fichier joint), soit --script (texte sur stdin), "
                              "un seul à la fois (pas les deux ni aucun).")
    if cmd in ("prepare", "make") and args.recu is not None:  # todo #437: a stored name, never a path from the page
        try:
            received = recus.chemin(args.recu, recus.dossier(settings))
            if recus.genre(args.recu) not in ("audio", "video"):
                raise recus.Refuse("Ce fichier joint n'est ni un audio ni une vidéo.")
        except recus.Refuse as exc:
            return envelope(status="error", summary="fichier refusé", error=str(exc), command=command)
        args.audio = str(received)
    if cmd == "lire":  # todo #439: text on stdin, never argv; no montage lock (it touches no montage)
        try:
            data = lecture.read_aloud(settings, sys.stdin.read(tts.LIRE_MAX_CHARS * 4 + 1))
        except UltronHubError as exc:
            return envelope(status="error", summary="lecture impossible", error=str(exc), command=command)
        return envelope(summary="lecture prête", command=command, data=data)
    if cmd == "keep":
        return do_keep(settings, args.id)
    if cmd == "mesure":
        return do_mesure(settings, args.id, args.version)
    try:
        with store.single_montage(settings):
            if cmd == "render":
                if args.apply is not None and not re.fullmatch(r"[0-9a-f]{12}", args.apply):
                    return envelope(status="error", summary="empreinte invalide", command=command,
                                    error="Cette retouche n'est plus d'actualité : redemande-la.")
                return do_render(settings, args.id, args.requested_by, args.apply)
            if cmd == "retouch":
                return do_retouch(settings, args)
            if cmd == "photos":  # captions on stdin, never argv: the user's free text
                return do_photos(settings, args.id, args.files,
                                 "" if sys.stdin.isatty() else sys.stdin.read(64 * 1024), args.requested_by)
            if cmd == "fix":  # stdin, never argv: the user's free text
                return do_fix(settings, args.id, sys.stdin.read(plan.FIX_MAX_CHARS * 4 + 1), args.requested_by)
            script = None
            if args.script:
                script = sys.stdin.read(tts.MAX_CHARS * 4 + 1)
            prepared = do_prepare(settings, args.audio, args.requested_by, script, args.style, args.fin)
            if cmd == "prepare" or prepared["status"] != "ok":
                return {**prepared, "command": command}
            rendered = do_render(settings, prepared["data"]["id"], args.requested_by)
            return {**rendered, "command": command, "data": {**prepared["data"], **(rendered["data"] or {})}}
    except store.Busy:
        return envelope(status="error", summary="déjà en cours", error=store.BUSY_TEXT, data={"busy": True},
                        command=command)
