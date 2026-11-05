"""Words of the voice, with their timestamps, by the Whisper of the hermes-video skill (`whisper_words.py`, run in
a subprocess: the skill reads its own key). The words are UNTRUSTED data: they only end up as plain text."""

import json
import re
from pathlib import Path
from typing import Any

from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import Settings
from ultron_hub.montage import tools
from ultron_hub.montage.tools import MontageError

log = get_logger("montage")

HELPER = Path(__file__).with_name("whisper_words.py")
TIMEOUT_S = 5 * 60
FULL_TIMEOUT_S = 10 * 60   # documents: a long recording, all of it
MAX_WORDS = 400
WORD_CHARS = 40
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_PUNCT_ONLY_RE = re.compile(r"[\W_]+")


def merge_words(raw: Any, duration: float, max_words: int | None = MAX_WORDS) -> list[dict[str, Any]]:
    """Whisper tokens -> words: a token without a leading space (« 'hui, ») or made of punctuation only (« ! »)
    joins the previous word. Times are clamped to the audio and kept increasing. `max_words=None`: no cap."""
    words: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        token = _CTRL_RE.sub(" ", str(item.get("w") or ""))
        try:
            start, end = float(item.get("s")), float(item.get("e"))
        except (TypeError, ValueError):
            continue
        text = " ".join(token.split())
        if not text:
            continue
        start = min(max(0.0, start), duration)
        end = min(max(start, end), duration)
        glued = words and (not token[:1].isspace() or _PUNCT_ONLY_RE.fullmatch(text))
        if glued:
            words[-1]["w"] = (words[-1]["w"] + text)[:WORD_CHARS]
            words[-1]["e"] = max(words[-1]["e"], end)
            continue
        if words and start < words[-1]["s"]:
            start = words[-1]["s"]
        words.append({"w": text[:WORD_CHARS], "s": round(start, 2), "e": round(max(start, end), 2)})
        if max_words is not None and len(words) >= max_words:
            break
    return words


def transcribe(settings: Settings, wav: Path, duration: float, full: bool = False) -> dict[str, Any]:
    """{"backend", "language", "words"} for the cleaned voice, or MontageError.

    `full=True` (documents, todo #155): every word, no 400-word cap, up to FULL_TIMEOUT_S, generic error messages."""
    timeout_s = FULL_TIMEOUT_S if full else TIMEOUT_S
    src = settings.video_skill_dir / "src"
    if not (src / "watch" / "config.py").is_file():
        raise MontageError("Le Whisper du skill hermes-video est introuvable sur le serveur.")
    code, out, err = tools.run([tools.PYTHON, str(HELPER), "--skill-src", str(src), str(wav)], timeout_s)
    if code is None:
        raise MontageError(f"La transcription a dépassé {int(timeout_s // 60)} minutes.")
    try:
        data = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    except ValueError:
        data = {}
    if code != 0 or not isinstance(data, dict) or "words" not in data:
        log.warning("montage transcription failed (exit %s): %s / %s", code, str(data.get("error") if isinstance(
            data, dict) else "")[:200], err[-300:])
        raise MontageError("La transcription a échoué. Réessaie dans un moment.")
    words = merge_words(data["words"], duration, None if full else MAX_WORDS)
    if len(words) < 3:
        if full:
            raise MontageError("Je n'ai presque rien compris dans cet audio (pas de parole ?).")
        raise MontageError("Je n'ai presque rien compris dans cet audio : réessaie en parlant plus près du micro.")
    return {"backend": str(data.get("backend") or "")[:20], "language": str(data.get("language") or "")[:10],
            "words": words}
