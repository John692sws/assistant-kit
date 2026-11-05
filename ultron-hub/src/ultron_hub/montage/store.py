"""var/montage/<id>/: the files of one montage (0700 folder, 0600 files).

    voice.wav     the cleaned voice (silences cut)      words.json  the words and their timestamps (untrusted)
    plan.json     the checked plan (title, scenes)      props.json  what Remotion reads
    v1.mp4, v2.mp4...  the rendered versions (todo #75)  meta.json   {"kept": true} after 📚 Garder
    pending.json  a retouch waiting for its ✅           voice.next.wav  the voice of a pending ⏱ retouch
    images.json, images/p1.jpg...  the user's photos, re-encoded (todo #76, photos.py)
"""

import fcntl
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ultron_hub.common.settings import Settings
from ultron_hub.montage.tools import MontageError

MONTAGE_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")
WORDS, PLAN, VOICE, META = "words.json", "plan.json", "voice.wav", "meta.json"
VERSION_RE = re.compile(r"v([1-9][0-9]{0,3})\.mp4")
BUSY_TEXT = "Un montage est déjà en cours. Réessaie dans quelques minutes."


class Busy(Exception):
    pass


def valid_id(montage_id: Any) -> bool:
    return isinstance(montage_id, str) and MONTAGE_ID_RE.fullmatch(montage_id) is not None


def root(settings: Settings) -> Path:
    return settings.var_dir / "montage"


def folder(settings: Settings, montage_id: str) -> Path:
    """The id is validated before it becomes part of a path."""
    if not valid_id(montage_id):
        raise MontageError("Identifiant de montage invalide.")
    return root(settings) / montage_id


def create(settings: Settings, montage_id: str) -> Path:
    path = folder(settings, montage_id)
    root(settings).mkdir(mode=0o700, parents=True, exist_ok=True)
    path.mkdir(mode=0o700)
    return path


def write(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    tmp.replace(path)


def read(settings: Settings, montage_id: str, name: str) -> Any:
    path = folder(settings, montage_id) / name
    if path.is_symlink() or not path.is_file():
        raise MontageError("Ce montage n'existe plus (ou n'est pas prêt). Renvoie la note vocale.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise MontageError("Les données de ce montage sont illisibles.") from None


def versions(path: Path) -> list[int]:
    """Numbers of the rendered versions in a montage folder, in order (regular files only)."""
    found = []
    for item in path.iterdir() if path.is_dir() else []:
        m = VERSION_RE.fullmatch(item.name)
        if m and item.is_file() and not item.is_symlink():
            found.append(int(m.group(1)))
    return sorted(found)


def video_path(path: Path, version: int) -> Path:
    return path / f"v{version}.mp4"


def read_meta(settings: Settings, montage_id: str) -> dict[str, Any]:
    """meta.json, or {} when the montage has none yet."""
    path = folder(settings, montage_id) / META
    if not path.exists():
        return {}
    data = read(settings, montage_id, META)
    return data if isinstance(data, dict) else {}


@contextmanager
def single_montage(settings: Settings) -> Iterator[None]:
    """Non-blocking exclusive lock (var/montage/.lock): a second montage while one runs is refused, not queued."""
    root(settings).mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(root(settings) / ".lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy from None
        yield
