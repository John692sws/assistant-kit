"""`ultron-hub montage lire` (todo #439): the dashboard's 🔊 Lire, spoken by the montage's own voice (Piper, tts.synthesize).

The text arrives on stdin (never argv), is synthesized into a 0600 `<stamp>-<hex>-lecture.wav` of var/notifs/pj/ (served
by the existing GET /api/pj/<name>, audio with Range), and every such file older than an hour is removed at each call."""

import os
import secrets
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from ultron_hub import notifs
from ultron_hub.montage import tts
from ultron_hub.montage.tools import MontageError

SUFFIX = "-lecture.wav"
KEEP_S = 3600


def is_lecture(name: str) -> bool:
    return name.endswith(SUFFIX)


def clean(pj: Path, now: float | None = None) -> None:
    """Removes the readings older than KEEP_S (best effort)."""
    limit = (now if now is not None else time.time()) - KEEP_S
    try:
        for f in pj.iterdir():
            if is_lecture(f.name) and f.stat().st_mtime < limit:
                f.unlink(missing_ok=True)
    except OSError:
        pass


def read_aloud(settings, text: str) -> dict[str, str]:
    """Synthesizes `text` (checked, capped); returns the stored name and its /api/pj/ url."""
    clean_text = tts.check_text(text, tts.LIRE_MAX_CHARS)
    pj = notifs.pj_dir(settings)
    pj.mkdir(parents=True, exist_ok=True, mode=0o700)
    clean(pj)
    name = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}{SUFFIX}"
    with tempfile.TemporaryDirectory(dir=pj.parent, prefix=".lecture-") as tmp:  # 0700; tts.txt lives here and is erased
        wav = Path(tmp) / "lecture.wav"
        tts.synthesize(clean_text, wav)
        fd = os.open(pj / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(wav.read_bytes())
    if not notifs.PJ_RE.fullmatch(name):
        raise MontageError("Lecture impossible.")
    return {"nom": name, "url": notifs.PJ_URL + name}
