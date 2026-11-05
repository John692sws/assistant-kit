"""Retouches of a rendered montage (todo #75): one scene redone, another theme, a shorter or faster video.

A retouch is only PROPOSED: it is written to pending.json (with the voice it needs) and shown on an approval card;
`montage render <id> --apply <empreinte>` promotes exactly that proposal (fingerprint of pending.json) and renders a
new version (v2.mp4, v3.mp4, ...). The current plan never changes before the ✅. Every choice comes from a closed list
fixed here; Claude only rewrites the one scene asked (task montage-scene, closed schema, words = untrusted data).
"""

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import Settings
from ultron_hub.montage import audio, plan, store, tools
from ultron_hub.montage.tools import MontageError

log = get_logger("montage")

PENDING = "pending.json"
NEXT_VOICE = "voice.next.wav"
FP_CHARS = 12
# ⏱ Durée: closed choices -> (seconds cut at the start, seconds cut at the end, speed).
DUREE_CHOICES = {
    "debut-2": (2.0, 0.0, 1.0), "debut-5": (5.0, 0.0, 1.0),
    "fin-2": (0.0, 2.0, 1.0), "fin-5": (0.0, 5.0, 1.0),
    "vitesse-110": (0.0, 0.0, 1.1), "vitesse-125": (0.0, 0.0, 1.25),
}
DUREE_LABELS = {"debut-2": "2 s de moins au début", "debut-5": "5 s de moins au début",
                "fin-2": "2 s de moins à la fin", "fin-5": "5 s de moins à la fin",
                "vitesse-110": "voix 10 % plus rapide", "vitesse-125": "voix 25 % plus rapide"}
MIN_DURATION_S = 3.0
PURGE_DAYS = 30


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:FP_CHARS]


def write_pending(folder: Path, data: dict[str, Any]) -> str:
    store.write(folder / PENDING, data)
    return fingerprint((folder / PENDING).read_bytes())


def take_pending(folder: Path, expected: str) -> dict[str, Any]:
    """The proposal the user approved (same fingerprint as on the card), or MontageError."""
    path = folder / PENDING
    if path.is_symlink() or not path.is_file() or fingerprint(path.read_bytes()) != expected:
        raise MontageError("Cette retouche n'est plus d'actualité : redemande-la.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise MontageError("Cette retouche est illisible : redemande-la.") from None
    if not isinstance(data, dict) or data.get("kind") not in ("scene", "style", "duree"):
        raise MontageError("Cette retouche est illisible : redemande-la.")
    return data


def apply_pending(folder: Path, data: dict[str, Any]) -> None:
    """Promotes the approved proposal: plan (and, for ⏱, the new voice and words) become the current ones."""
    if data["kind"] == "duree":
        voice = folder / NEXT_VOICE
        if voice.is_symlink() or not voice.is_file():
            raise MontageError("La voix retouchée a disparu : redemande la retouche.")
        voice.replace(folder / store.VOICE)
        store.write(folder / store.WORDS, data["words"])
    store.write(folder / store.PLAN, data["plan"])
    (folder / PENDING).unlink(missing_ok=True)


# --- 🎨 Style --------------------------------------------------------------------------------------

def with_theme(current: dict[str, Any], theme: str) -> dict[str, Any]:
    if theme not in plan.THEMES:
        raise MontageError("Style inconnu.")
    if theme == current.get("theme"):
        raise MontageError("La vidéo a déjà ce style.")
    return {**current, "theme": theme}


# --- 🔁 Refaire une scène -------------------------------------------------------------------------

def window(current: dict[str, Any], n: int) -> tuple[int, int]:
    """Indices of the words scene n may start on: after the previous scene's word, before the next one's."""
    scenes = current["scenes"]
    low = scenes[n - 1]["mot"] + 1 if n > 0 else 0
    high = scenes[n + 1]["mot"] - 1 if n + 1 < len(scenes) else 10 ** 6
    return low, high


def scene_metrics(current: dict[str, Any], words: list[dict[str, Any]], n: int,
                  photos: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """What montage-scene receives in {metrics}: the words around the scene (untrusted), the scene to replace and
    its neighbours (written by ultron-hub from the checked plan), and the user's photos (todo #76)."""
    if not 0 <= n < len(current["scenes"]):
        raise MontageError("Cette scène n'existe pas.")
    low, high = window(current, n)
    high = min(high, len(words) - 1)
    scenes = current["scenes"]

    def describe(s):
        b = plan.bare(s)
        return {"mot": b["mot"], "type": b["type"], "texte": b["text"], "points": b["points"],
                "emotion": b["emotion"], "style": b["style"], "visuel": b["visuel"], "mots": b["mots"]}
    return {"mots_permis": f"{low} à {high}",
            "mots": "\n".join(f"{i} [{words[i]['s']:.2f}] {words[i]['w']}" for i in range(low, high + 1)),
            "scene_actuelle": describe(scenes[n]),
            "scene_avant": describe(scenes[n - 1]) if n > 0 else None,
            "scene_apres": describe(scenes[n + 1]) if n + 1 < len(scenes) else None,
            "photos": plan.photo_list(photos)}


def replace_scene(current: dict[str, Any], words: list[dict[str, Any]], n: int, answer: Any,
                  photos: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Scene n replaced by Claude's proposal (checked against the catalogue); every other scene stays the same. A
    trigger word outside the scene's window is brought back to the old one."""
    raw = answer.get("scene") if isinstance(answer, dict) else None
    if isinstance(raw, dict):
        low, high = window(current, n)
        if not isinstance(raw.get("mot"), int) or isinstance(raw.get("mot"), bool) or not low <= raw["mot"] <= high:
            raw = {**raw, "mot": current["scenes"][n]["mot"]}
    new = plan.build_scene(raw, words, plan.photo_sources(photos))
    old = plan.bare(current["scenes"][n])
    if new is None:
        raise MontageError("La nouvelle scène proposée est inutilisable. Réessaie.")
    if all(new[k] == old[k] for k in plan.SCENE_KEYS):
        raise MontageError("La scène proposée est identique à l'ancienne. Réessaie.")
    kept = [plan.bare(s) for s in current["scenes"]]
    kept[n] = new
    timed = plan.time_scenes(kept, words, current["duration"])
    if len(timed) != len(current["scenes"]):  # the new scene came too close to a neighbour: keep the old word
        kept[n]["mot"] = old["mot"]
        timed = plan.time_scenes(kept, words, current["duration"])
    return {**current, "scenes": timed}


# --- ⏱ Durée --------------------------------------------------------------------------------------

def retime(current: dict[str, Any], heard: dict[str, Any], choice: str) -> tuple[dict, dict, tuple]:
    """New plan and words for a ⏱ choice, and the ffmpeg cut (start, end, speed). Words cut away disappear with
    their scenes; MontageError when fewer than 3 scenes or 3 s would remain."""
    if choice not in DUREE_CHOICES:
        raise MontageError("Choix de durée inconnu.")
    cut_start, cut_end, speed = DUREE_CHOICES[choice]
    duration = float(current["duration"])
    end = duration - cut_end
    new_duration = round((end - cut_start) / speed, 2)
    if new_duration < MIN_DURATION_S:
        raise MontageError("Trop court : la vidéo durerait moins de 3 s.")
    words, mapping = [], {}
    for i, w in enumerate(heard["words"]):
        if w["s"] >= cut_start - 0.01 and w["e"] <= end + 0.01:
            mapping[i] = len(words)
            words.append({"w": w["w"], "s": round(max(0.0, (w["s"] - cut_start) / speed), 2),
                          "e": round(max(0.0, (min(w["e"], end) - cut_start) / speed), 2)})
    scenes = [{**plan.bare(s), "mot": mapping[s["mot"]],
               "mots": [mapping[i] for i in plan.bare(s)["mots"]] if all(i in mapping for i in plan.bare(s)["mots"])
               else []} for s in current["scenes"] if s["mot"] in mapping]
    timed = plan.time_scenes(scenes, words, new_duration)
    if len(timed) < plan.MIN_SCENES:
        raise MontageError("Trop coupé : il resterait moins de 3 animations. Choisis une coupe plus petite.")
    return ({**current, "scenes": timed, "duration": new_duration},
            {**heard, "words": words, "duration": new_duration}, (cut_start, end, speed))


def cut_voice(folder: Path, cut: tuple[float, float, float]) -> None:
    start, end, speed = cut
    filters = f"atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS"
    if speed != 1.0:
        filters += f",atempo={speed:.2f}"
    dest = folder / NEXT_VOICE
    code, _, err = tools.run([tools.FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-y", "-i",
                              str(folder / store.VOICE), "-af", filters, "-c:a", "pcm_s16le", str(dest)],
                             audio.FFMPEG_TIMEOUT_S)
    if code != 0 or not dest.is_file():
        log.warning("ffmpeg retime failed (exit %s): %s", code, err[-300:])
        raise MontageError("La retouche de la voix a échoué.")


# --- history ---------------------------------------------------------------------------------------

def purge(settings: Settings, days: int = PURGE_DAYS, now: float | None = None) -> list[str]:
    """Removes the montages older than `days` that were not kept (📚). Only folders named like a montage id, never a
    symlink. Returns the removed ids. Best effort: an unreadable folder is left alone."""
    root = store.root(settings)
    if not root.is_dir():
        return []
    limit = (now if now is not None else time.time()) - days * 86400
    removed = []
    for path in sorted(root.iterdir()):
        if not store.valid_id(path.name) or path.is_symlink() or not path.is_dir():
            continue
        try:
            if path.stat().st_mtime > limit or store.read_meta(settings, path.name).get("kept"):
                continue
            shutil.rmtree(path)
            removed.append(path.name)
        except (OSError, MontageError):
            log.warning("montage purge: %s left alone", path.name)
    if removed:
        log.info("montage purge: %d montage(s) older than %d days removed", len(removed), days)
    return removed
