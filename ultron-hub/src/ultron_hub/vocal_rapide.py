"""Fast transcription of the user's own voice messages sent from the dashboard (todo #397).

No `docs` batch and no Sonnet job: the session only needs the raw words. ffmpeg decodes to 16 kHz mono WAV (faster-whisper's
own `decode_audio` is broken in the .venv), then faster-whisper "base" (cpu, int8, beam 1, French, no word timestamps) runs
in-process: about 6 s for 1 min 34 s of speech instead of 42 s through the word-level pipeline.
"""

from __future__ import annotations

import tempfile
import wave
from pathlib import Path
from typing import Any, Callable

from ultron_hub.bot.voice import NO_LIB, VoiceError
from ultron_hub.docs.collect import MAX_AUDIO_S, duration_label

MODEL = "base"
DEVICE = "cpu"
COMPUTE_TYPE = "int8"
LANGUAGE = "fr"
BEAM_SIZE = 1
MAX_CHARS = 20000


def _default_factory() -> Any:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise VoiceError(NO_LIB) from None
    return WhisperModel(MODEL, device=DEVICE, compute_type=COMPUTE_TYPE)


def _to_wav16k(src: Path, dest: Path) -> None:
    """16 kHz mono WAV (what Whisper expects; montage.audio.to_wav is 48 kHz)."""
    from ultron_hub.montage import tools
    from ultron_hub.montage.tools import MontageError
    code, _, _ = tools.run([tools.FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(src), "-vn", "-ac", "1",
                            "-ar", "16000", "-c:a", "pcm_s16le", str(dest)], 120)
    if code != 0 or not dest.is_file():
        raise MontageError("Je n'arrive pas à lire cet audio.")


def _samples(wav_path: Path) -> Any:
    import numpy as np
    with wave.open(str(wav_path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def transcrire(path: Path, model_factory: Callable[[], Any] | None = None) -> str:
    """Raw French text of an audio file. Raises VoiceError (message in French, for the user)."""
    from ultron_hub.montage import audio
    from ultron_hub.montage.tools import MontageError
    try:
        with tempfile.TemporaryDirectory(prefix="vocal-") as tmp:
            info = audio.probe(path)
            if not any(s.get("codec_type") == "audio" for s in info["streams"]):
                raise VoiceError("Ce fichier ne contient pas de son.")
            if info["duration"] > MAX_AUDIO_S:
                raise VoiceError(f"Audio trop long ({duration_label(info['duration'])}) : 20 minutes au maximum.")
            wav = Path(tmp) / "audio.wav"
            _to_wav16k(path, wav)
            model = (model_factory or _default_factory)()
            try:
                segments, _ = model.transcribe(_samples(wav), language=LANGUAGE, beam_size=BEAM_SIZE,
                                               word_timestamps=False)
                text = " ".join(seg.text.strip() for seg in segments)
            except VoiceError:
                raise
            except Exception as exc:  # noqa: BLE001 - only the kind of failure, never the content
                raise VoiceError(f"La transcription a échoué ({type(exc).__name__}).") from None
    except MontageError as exc:
        raise VoiceError(str(exc)) from None
    text = " ".join(text.split())[:MAX_CHARS]
    if not text:
        raise VoiceError("Je n'entends pas de parole dans ce vocal.")
    return text
