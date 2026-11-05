"""Voice preparation with ffmpeg: any audio -> mono WAV, silences found by `silencedetect` and cut.

The word timestamps come from the CLEANED audio (it is transcribed after the cut), so nothing has to be shifted.
"""

import json
import re
from pathlib import Path

from ultron_hub.common.logging import get_logger
from ultron_hub.montage import tools
from ultron_hub.montage.tools import MontageError

log = get_logger("montage")

MAX_INPUT_BYTES = 25 * 1024 * 1024
MAX_RAW_S = 120.0       # a Telegram voice note of 60 s, with some margin
MAX_CLEAN_S = 60.0      # 8 GB of RAM: renders of 60 s at most (todo #69)
MIN_CLEAN_S = 2.0
SILENCE_DB = -35
SILENCE_MIN_S = 0.7     # shorter pauses are breathing or punctuation, they stay
KEEP_PAD_S = 0.15       # kept on each side of a cut silence (a cut pause lasts 0.3 s)
SAMPLE_RATE = 48000
FFMPEG_TIMEOUT_S = 120
_SILENCE_RE = re.compile(r"silence_(start|end): (-?[0-9.]+)")


def probe(path: Path) -> dict:
    """Duration and streams of a media file (ffprobe), or MontageError."""
    code, out, _ = tools.run([tools.FFPROBE, "-v", "error", "-show_entries",
                              "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate",
                              "-of", "json", str(path)], 60)
    try:
        data = json.loads(out) if code == 0 else None
        duration = float(data["format"]["duration"])
    except (TypeError, ValueError, KeyError):
        raise MontageError("Ce fichier n'est pas un audio lisible.") from None
    return {"duration": duration, "streams": data.get("streams") or []}


def check_input(path: Path) -> Path:
    """The audio given to `montage prepare`: a regular file (no symlink), not too big, with a sound track."""
    try:
        path = Path(path).absolute()
        if path.is_symlink() or not path.is_file():
            raise MontageError("Fichier audio introuvable.")
        if path.stat().st_size > MAX_INPUT_BYTES:
            raise MontageError("Fichier audio trop gros (25 Mo au maximum).")
    except OSError:
        raise MontageError("Fichier audio illisible.") from None
    info = probe(path)
    if not any(s.get("codec_type") == "audio" for s in info["streams"]):
        raise MontageError("Ce fichier ne contient pas de son.")
    if info["duration"] > MAX_RAW_S:
        raise MontageError(f"Audio trop long ({int(info['duration'])} s) : une minute de parole au maximum.")
    return path


def to_wav(src: Path, dest: Path) -> None:
    code, _, err = tools.run([tools.FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(src),
                              "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(dest)],
                             FFMPEG_TIMEOUT_S)
    if code != 0 or not dest.is_file():
        log.warning("ffmpeg to_wav failed (exit %s): %s", code, err[-300:])
        raise MontageError("Je n'arrive pas à lire cet audio.")


def silences(wav: Path) -> list[tuple[float, float]]:
    """[(start, end)] of the silences longer than SILENCE_MIN_S (an open silence ends at the end of the file)."""
    code, _, err = tools.run([tools.FFMPEG, "-hide_banner", "-nostdin", "-i", str(wav), "-af",
                              f"silencedetect=noise={SILENCE_DB}dB:d={SILENCE_MIN_S}", "-f", "null", "-"],
                             FFMPEG_TIMEOUT_S)
    if code != 0:
        raise MontageError("L'analyse des silences a échoué.")
    found, start = [], None
    for kind, value in _SILENCE_RE.findall(err):
        if kind == "start":
            start = max(0.0, float(value))
        elif start is not None:
            found.append((start, float(value)))
            start = None
    if start is not None:
        found.append((start, float("inf")))
    return found


def keep_ranges(duration: float, cuts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The parts of the audio to keep: everything but the silences, each one shortened to 2 x KEEP_PAD_S."""
    ranges, pos = [], 0.0
    for start, end in sorted(cuts):
        cut_from = 0.0 if start <= 0.05 else start + KEEP_PAD_S
        cut_to = min(duration, end - KEEP_PAD_S) if end != float("inf") else duration
        if cut_to - cut_from <= 0.05:
            continue
        if cut_from > pos:
            ranges.append((round(pos, 3), round(cut_from, 3)))
        pos = max(pos, cut_to)
    if duration - pos > 0.05:
        ranges.append((round(pos, 3), round(duration, 3)))
    return ranges


def cut(wav: Path, dest: Path, ranges: list[tuple[float, float]]) -> None:
    select = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in ranges)
    code, _, err = tools.run([tools.FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(wav), "-af",
                              f"aselect='{select}',asetpts=N/SR/TB", "-c:a", "pcm_s16le", str(dest)],
                             FFMPEG_TIMEOUT_S)
    if code != 0 or not dest.is_file():
        log.warning("ffmpeg cut failed (exit %s): %s", code, err[-300:])
        raise MontageError("Le nettoyage des silences a échoué.")


def clean_voice(src: Path, folder: Path) -> dict:
    """src -> folder/raw.wav -> folder/voice.wav without long silences. Returns durations (raw, clean, removed)."""
    raw = folder / "raw.wav"
    voice = folder / "voice.wav"
    to_wav(src, raw)
    raw_s = probe(raw)["duration"]
    ranges = keep_ranges(raw_s, silences(raw))
    spoken = sum(b - a for a, b in ranges)
    if spoken < MIN_CLEAN_S:
        raise MontageError("Je n'entends presque rien dans cet audio : parle un peu plus fort ou plus longtemps.")
    cut(raw, voice, ranges)
    clean_s = probe(voice)["duration"]
    if clean_s > MAX_CLEAN_S:
        raise MontageError(f"Trop long une fois les silences retirés ({int(clean_s)} s) : 60 s au maximum.")
    raw.unlink(missing_ok=True)
    return {"raw_s": round(raw_s, 2), "clean_s": round(clean_s, 2), "removed_s": round(max(0.0, raw_s - clean_s), 2)}
