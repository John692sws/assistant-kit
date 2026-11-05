"""Free stock photos for a montage (todo #78): Pexels search by a few English words chosen by Claude.

The API key is read from ~/.config/ultron-hub/pexels.key (0600, path from pwd, never from $HOME): never shown,
logged nor given to Claude. Without a key the scene type `photo` is not offered and everything else works.
A search returns the first portrait photo; it is downloaded from images.pexels.com only (https, no redirect, 10 MB,
20 s), checked and re-encoded by photos.to_jpeg into var/montage/<id>/images/s-<hash>.jpg. Any failure turns the
scene into a key word: the montage never fails because of a photo.
"""

import hashlib
import json
import os
import pwd
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ultron_hub.common.logging import get_logger
from ultron_hub.montage import photos, plan
from ultron_hub.montage.tools import MontageError

log = get_logger("montage")

KEY_PATH = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config" / "ultron-hub" / "pexels.key"
KEY_RE = re.compile(r"[A-Za-z0-9]{20,100}")
API = "https://api.pexels.com/v1/search"
IMAGE_HOST = "images.pexels.com"
MAX_STOCK = 3
SEARCH_TIMEOUT_S = 15
DOWNLOAD_TIMEOUT_S = 20
MAX_ANSWER_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
CREDIT_MAX = 40
USER_AGENT = "ultron-hub-montage/1.0"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None   # a redirect becomes an HTTPError: we only talk to the two hosts we know


_OPENER = urllib.request.build_opener(_NoRedirect)


def api_key(path: Path | None = None) -> str | None:
    """The key, or None (absent, a link, unreadable or malformed)."""
    path = path or KEY_PATH
    try:
        if path.is_symlink() or not path.is_file():
            return None
        key = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    return key if KEY_RE.fullmatch(key) else None


def available() -> bool:
    return api_key() is not None


def valid_query(query: Any) -> bool:
    return isinstance(query, str) and len(query) <= plan.VISUEL_MAX and plan.STOCK_QUERY_RE.fullmatch(query) is not None


def _get(url: str, headers: dict[str, str], timeout_s: float, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    with _OPENER.open(request, timeout=timeout_s) as answer:
        data = answer.read(limit + 1)
    if len(data) > limit:
        raise MontageError("Réponse trop grosse.")
    return data


def search(query: str, key: str) -> dict[str, str] | None:
    """{"url", "credit"} of the first portrait photo for `query`, or None."""
    url = API + "?" + urllib.parse.urlencode({"query": query, "orientation": "portrait", "per_page": 5})
    data = json.loads(_get(url, {"Authorization": key}, SEARCH_TIMEOUT_S, MAX_ANSWER_BYTES))
    for item in data.get("photos") or [] if isinstance(data, dict) else []:
        src = item.get("src") if isinstance(item, dict) else None
        link = src.get("large2x") or src.get("large") if isinstance(src, dict) else None
        if isinstance(link, str) and _allowed(link):
            return {"url": link, "credit": plan.clean(item.get("photographer"), CREDIT_MAX) or "Pexels"}
    return None


def _allowed(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" and parts.hostname == IMAGE_HOST and parts.port is None and not parts.username


def fetch(folder: Path, query: str, key: str) -> dict[str, str]:
    """Downloads and re-encodes the photo for `query`: {"src", "credit"}, or MontageError."""
    try:
        found = search(query, key)
        if found is None:
            raise MontageError("Aucune photo trouvée.")
        raw = _get(found["url"], {}, DOWNLOAD_TIMEOUT_S, MAX_IMAGE_BYTES)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("pexels: %s for a montage photo", type(exc).__name__)   # never the key, never the URL
        raise MontageError("Photo libre introuvable.") from None
    images = folder / photos.IMAGES_DIR
    if images.is_symlink():
        raise MontageError("Dossier d'images invalide.")
    images.mkdir(mode=0o700, exist_ok=True)
    name = "s-" + hashlib.sha256(query.encode()).hexdigest()[:12]
    tmp = images / (name + ".download")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(raw)
    try:
        photos.probe_image(tmp)
        photos.to_jpeg(tmp, images / (name + ".jpg"))
        (images / (name + ".jpg")).chmod(0o600)
    finally:
        tmp.unlink(missing_ok=True)
    return {"src": f"{photos.IMAGES_DIR}/{name}.jpg", "credit": found["credit"]}


def resolve(folder: Path, plan_data: dict[str, Any]) -> dict[str, Any]:
    """Fetches the photo of every `photo` scene that has none yet (at most MAX_STOCK). A scene whose photo cannot be
    found, or any photo scene when there is no key, becomes a key word. Returns the plan (changed in place)."""
    key = api_key()
    done = 0
    for scene in plan_data["scenes"]:
        if scene["type"] != "photo" or scene.get("src"):
            done += scene["type"] == "photo"
            continue
        try:
            if key is None or done >= MAX_STOCK or not valid_query(scene.get("visuel")):
                raise MontageError("Photo libre indisponible.")
            scene.update(fetch(folder, scene["visuel"], key))
            done += 1
        except MontageError:
            scene.update(type="mot_cle", visuel="", src="", credit="", text=scene["text"] or scene.get("word") or "…")
    return plan_data
