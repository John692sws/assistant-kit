"""healthchecks.io pings: an external dead man's switch that alerts when the server goes quiet.

The ping URLs live in ~/.config/ultron-hub/healthchecks.env (0600, outside the repository):
    HC_VIVANT_URL=https://hc-ping.com/<uuid>
    HC_SAUVEGARDE_URL=https://hc-ping.com/<uuid>
A ping never raises: a network failure is returned as text, healthchecks.io alerts on silence anyway.
"""

import os
import pwd
import re
import urllib.error
import urllib.request
from pathlib import Path

PING_TIMEOUT_S = 10
BODY_MAX_BYTES = 10_000  # healthchecks.io keeps the first 10 kB of a ping body
URL_RE = re.compile(r"https://[A-Za-z0-9.-]+(?::\d+)?/[A-Za-z0-9/_.-]+")
KEYS = ("HC_VIVANT_URL", "HC_SAUVEGARDE_URL")


def real_home() -> Path:
    # From pwd, not $HOME: a process started with HOME=/tmp/x must not pick another config.
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def env_path() -> Path:
    return real_home() / ".config" / "ultron-hub" / "healthchecks.env"


def load_urls(path: Path | None = None) -> dict[str, str]:
    """Known keys with a valid https URL; anything else is ignored. Missing file -> {}."""
    path = path or env_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    urls = {}
    for line in lines:
        key, sep, value = line.strip().partition("=")
        value = value.strip().strip("'\"").rstrip("/")
        if sep and key.strip() in KEYS and URL_RE.fullmatch(value):
            urls[key.strip()] = value
    return urls


def ping(url: str, suffix: str = "", body: str = "", timeout: float = PING_TIMEOUT_S) -> str | None:
    """POST to <url>[/<suffix>]; returns None on success, else a short error text."""
    if suffix not in ("", "start", "fail"):
        return f"suffixe inconnu : {suffix}"
    target = f"{url}/{suffix}" if suffix else url
    data = body.encode("utf-8")[:BODY_MAX_BYTES]
    request = urllib.request.Request(target, data=data, method="POST",
                                     headers={"User-Agent": "ultron-hub"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return None if 200 <= response.status < 300 else f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}"
    except Exception as exc:  # DNS, timeout, TLS...: never fatal
        return f"{type(exc).__name__}: {exc}"
