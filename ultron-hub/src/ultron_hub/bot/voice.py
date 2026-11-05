"""Transcription des vocaux Telegram sous `ultron-hub bot run` (todo #195) : faster-whisper en local, sans cloud.

Mêmes réglages que Hermes (`stt:` de sa config, ~/notes/agent-ultron-recherche/messagerie.md §1) : modèle `small`, cpu,
int8, langue fr. Le modèle est chargé au premier vocal puis gardé ; une transcription à la fois (un thread, la boucle du
bot reste libre). `faster-whisper` est l'extra optionnel `voice` du pyproject (jamais importé au chargement du module).
Le texte transcrit est une donnée non fiable : il ne devient qu'une demande écrite par l'utilisateur (🚀 Demande), jamais
une commande exécutée telle quelle.

Distinct de `montage/whisper_words.py` : celui-là tourne dans un sous-processus avec le Whisper du skill hermes-video
(mots horodatés, backend choisi par le skill) ; ici on veut seulement le texte, dans le processus du bot.
"""

import asyncio
import threading
from pathlib import Path
from typing import Any, Callable

MODEL = "small"
DEVICE = "cpu"
COMPUTE_TYPE = "int8"
LANGUAGE = "fr"
MAX_BYTES = 20 * 1024 * 1024  # limite de téléchargement d'un bot Telegram
MAX_CHARS = 8000

NO_LIB = ("faster-whisper absent du .venv d'ultron-hub : installer `.venv/bin/pip install -e '.[voice]'` pour "
          "transcrire les vocaux.")


class VoiceError(Exception):
    """Échec de transcription, message en français destiné à l'utilisateur (`user_facing`)."""

    user_facing = True


def _default_factory() -> Any:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise VoiceError(NO_LIB) from None
    return WhisperModel(MODEL, device=DEVICE, compute_type=COMPUTE_TYPE)


class Transcriber:
    """`await transcriber(path)` -> texte. `model_factory` : tests (faux modèle), sinon faster-whisper small."""

    def __init__(self, model_factory: Callable[[], Any] | None = None):
        self._factory = model_factory or _default_factory
        self._model: Any = None
        self._lock = threading.Lock()

    def transcribe(self, path: str | Path) -> str:
        path = Path(path)
        try:
            size = path.stat().st_size
        except OSError:
            raise VoiceError("Vocal introuvable sur le serveur.") from None
        if size > MAX_BYTES:
            raise VoiceError("Vocal trop gros (20 Mo au maximum).")
        with self._lock:  # une transcription à la fois, et un seul chargement du modèle
            if self._model is None:
                self._model = self._factory()
            try:
                segments, _info = self._model.transcribe(str(path), language=LANGUAGE, beam_size=5)
                text = " ".join(seg.text.strip() for seg in segments)
            except VoiceError:
                raise
            except Exception as exc:  # noqa: BLE001 - jamais le contenu, seulement le genre d'échec
                raise VoiceError(f"La transcription a échoué ({type(exc).__name__}).") from None
        return " ".join(text.split())[:MAX_CHARS]

    async def __call__(self, path: str | Path) -> str:
        return await asyncio.to_thread(self.transcribe, path)
