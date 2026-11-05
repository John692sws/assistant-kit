"""The user's photos in a montage (todo #76): files received from Telegram, untrusted.

Each file must be a regular file (no symlink), 20 MB at most, that ffprobe reads as ONE still image (JPEG, PNG or
WebP). ffmpeg decodes it and writes a NEW JPEG (metadata dropped, 1080x1440 at most) in var/montage/<id>/images/;
the original is never kept. images.json lists them: [{"id": "p1", "file": "images/p1.jpg", "legende": "..."}].
A new set of photos replaces the previous one.
"""

import json
import re
import shutil
from pathlib import Path
from typing import Any

from ultron_hub.common.logging import get_logger
from ultron_hub.montage import plan, store, tools
from ultron_hub.montage.tools import MontageError

log = get_logger("montage")

IMAGES = "images.json"
IMAGES_DIR = "images"
MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_SIDE = 12000
CODECS = {"mjpeg", "png", "webp"}
BOX_W, BOX_H = 1080, 1440
FFMPEG_TIMEOUT_S = 60
PHOTO_ID_RE = re.compile(r"p[1-5]")


def probe_image(path: Path) -> dict[str, Any]:
    """The single video stream of an image file, or MontageError."""
    code, out, _ = tools.run([tools.FFPROBE, "-v", "error", "-show_entries",
                              "stream=codec_type,codec_name,width,height", "-of", "json", str(path)], 30)
    try:
        streams = json.loads(out)["streams"] if code == 0 else None
    except (TypeError, ValueError, KeyError):
        streams = None
    if not isinstance(streams, list) or len(streams) != 1 or streams[0].get("codec_type") != "video":
        raise MontageError("Ce fichier n'est pas une photo lisible.")
    stream = streams[0]
    if stream.get("codec_name") not in CODECS:
        raise MontageError("Format de photo refusé : JPEG, PNG ou WebP seulement.")
    width, height = stream.get("width"), stream.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or not 0 < width <= MAX_SIDE \
            or not 0 < height <= MAX_SIDE:
        raise MontageError("Photo trop grande ou illisible.")
    return stream


def check_input(path: Path) -> Path:
    try:
        path = Path(path).absolute()
        if path.is_symlink() or not path.is_file():
            raise MontageError("Photo introuvable.")
        if path.stat().st_size > MAX_INPUT_BYTES:
            raise MontageError("Photo trop lourde (20 Mo au maximum).")
    except OSError:
        raise MontageError("Photo illisible.") from None
    probe_image(path)
    return path


def to_jpeg(src: Path, dest: Path) -> None:
    """A new JPEG drawn from the decoded pixels: no metadata, fits in BOX_W x BOX_H."""
    scale = f"scale=w={BOX_W}:h={BOX_H}:force_original_aspect_ratio=decrease:force_divisible_by=2"
    code, _, err = tools.run([tools.FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(src),
                              "-frames:v", "1", "-vf", scale, "-map_metadata", "-1", "-q:v", "3", "-f", "image2",
                              "-c:v", "mjpeg", str(dest)], FFMPEG_TIMEOUT_S)
    if code != 0 or not dest.is_file():
        log.warning("ffmpeg photo failed (exit %s): %s", code, err[-300:])
        raise MontageError("Une photo n'a pas pu être convertie.")


def captions(raw: str, count: int) -> list[str]:
    """Captions read on stdin (JSON list of strings, same order as the files), cleaned and shortened."""
    try:
        data = json.loads(raw) if raw.strip() else []
    except ValueError:
        raise MontageError("Légendes illisibles.") from None
    if not isinstance(data, list):
        raise MontageError("Légendes illisibles.")
    data = (data + [""] * count)[:count]
    return [plan.clean(c if isinstance(c, str) else "", plan.CAPTION_MAX) for c in data]


def import_photos(folder: Path, files: list[str], legendes: list[str]) -> list[dict[str, Any]]:
    """Checks and converts the files, then replaces the montage's photos. Nothing changes if one is refused."""
    if not 1 <= len(files) <= plan.MAX_PHOTOS:
        raise MontageError(f"De 1 à {plan.MAX_PHOTOS} photos.")
    sources = []
    for n, name in enumerate(files, 1):
        try:
            sources.append(check_input(Path(name)))
        except MontageError as exc:
            raise MontageError(f"Photo {n} : {exc}") from None
    staging = folder / (IMAGES_DIR + ".new")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(mode=0o700)
    try:
        photos = []
        for n, (src, legende) in enumerate(zip(sources, legendes), 1):
            to_jpeg(src, staging / f"p{n}.jpg")
            (staging / f"p{n}.jpg").chmod(0o600)
            photos.append({"id": f"p{n}", "file": f"{IMAGES_DIR}/p{n}.jpg", "legende": legende})
    except MontageError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    target = folder / IMAGES_DIR
    if target.is_symlink():
        target.unlink()
    shutil.rmtree(target, ignore_errors=True)
    staging.replace(target)
    store.write(folder / IMAGES, photos)
    return photos


def read(folder: Path) -> list[dict[str, Any]]:
    """The photos of a montage (images.json), only well-formed entries whose file is a regular file; [] if none."""
    path = folder / IMAGES
    if path.is_symlink() or not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    photos = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or not PHOTO_ID_RE.fullmatch(str(item.get("id"))):
            continue
        file = folder / IMAGES_DIR / f"{item['id']}.jpg"
        if file.is_symlink() or not file.is_file() or item.get("file") != f"{IMAGES_DIR}/{item['id']}.jpg":
            continue
        photos.append({"id": item["id"], "file": item["file"], "legende": str(item.get("legende") or "")})
    return photos
