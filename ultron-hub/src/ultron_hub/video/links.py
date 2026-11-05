"""Which video links /video accepts: https only, host in a closed list (exact match, never "contains")."""

import re
from urllib.parse import urlsplit

ALLOWED_HOSTS = frozenset({
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
})
MAX_URL_CHARS = 500
_BAD_CHARS = re.compile(r"[\s\x00-\x1f\x7f]")


def is_allowed(url: object) -> bool:
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS or _BAD_CHARS.search(url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and parts.hostname in ALLOWED_HOSTS and port in (None, 443)
            and parts.username is None and parts.password is None)
