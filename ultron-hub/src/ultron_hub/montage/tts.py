"""Local speech synthesis for `montage prepare --script` (todo #435): Piper, voice fr_FR-siwis-medium.

The text goes through a 0600 file of the montage folder, never through argv. Piper is installed by the
`montage_tts_install` action of ultron-user (own venv, pinned package, voice checked by SHA-256)."""

import os
import re
import unicodedata
from pathlib import Path

from ultron_hub.montage import tools
from ultron_hub.montage.tools import MontageError

PIPER_DIR = Path.home() / ".local" / "share" / "ultron-hub" / "piper"
PIPER_PYTHON = PIPER_DIR / "venv" / "bin" / "python"
VOICE = PIPER_DIR / "voix" / "fr_FR-siwis-medium.onnx"
VOICE_NAME = "piper:fr_FR-siwis-medium"
MAX_CHARS = 1200
LIRE_MAX_CHARS = 2500  # todo #439: 🔊 Lire of the dashboard (a whole answer), same voice
TIMEOUT_S = 120
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f  ]")
_SALAWAT = "\ufdfa"  # ﷺ
_BULLETS = set("•●○◦▪▫■□‣⁃·")
_KEEP_SYMBOLS = set("+=")


def clean_for_voice(text: str) -> str:
    """Todo #445: the text without what a voice cannot read: emojis, pictograms, arrows, decorative bullets and
    variation selectors go; ﷺ becomes « paix et salut sur lui »; letters of any language, digits, normal
    punctuation and accents stay; repeated spaces are folded (line breaks kept)."""
    out = []
    for ch in (text or "").replace(_SALAWAT, " paix et salut sur lui "):
        cat = unicodedata.category(ch)
        if ch in _BULLETS or ch in "\u200d\ufeff" or cat in ("So", "Sk", "Me", "Cs", "Co") or 0xFE00 <= ord(ch) <= 0xFE0F:
            out.append(" " if ch in _BULLETS else "")
        elif cat == "Sm" and ch not in _KEEP_SYMBOLS:
            out.append(" ")
        else:
            out.append(ch)
    return "\n".join(" ".join(line.split()) for line in "".join(out).split("\n"))


def check_text(text: str, max_chars: int = MAX_CHARS) -> str:
    """The script cleaned for the voice (clean_for_voice) and of control characters (line breaks kept), or
    MontageError. Single entry of --script (CLI and dashboard) and of `lire`; the subtitles use this same text."""
    clean = "\n".join(" ".join(line.split()) for line in _CTRL_RE.sub(" ", clean_for_voice(text)).replace("\r", "\n").split("\n"))
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    if not clean:
        raise MontageError("Le script est vide : écris le texte à faire lire.")
    if len(clean) > max_chars:
        raise MontageError(f"Script trop long ({len(clean)} caractères) : {max_chars} au maximum.")
    return clean


def synthesize(text: str, dest_wav: Path) -> None:
    """Reads `text` (already checked) with Piper into `dest_wav`."""
    if not PIPER_PYTHON.is_file() or not VOICE.is_file() or not Path(str(VOICE) + ".json").is_file():
        raise MontageError("La voix de synthèse n'est pas installée : lance l'action montage_tts_install.")
    source = dest_wav.parent / "tts.txt"
    fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    try:
        code, _, err = tools.run([str(PIPER_PYTHON), "-m", "piper", "-m", str(VOICE), "-i", str(source),
                                  "-f", str(dest_wav)], TIMEOUT_S, cwd=str(dest_wav.parent))
    finally:
        source.unlink(missing_ok=True)
    if code is None:
        raise MontageError("La voix de synthèse a pris trop de temps (2 minutes).")
    if code != 0 or not dest_wav.is_file() or dest_wav.stat().st_size == 0:
        raise MontageError("La voix de synthèse a échoué. Si elle vient d'être installée, relance montage_tts_install.")
