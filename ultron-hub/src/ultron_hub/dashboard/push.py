"""Web Push to the dashboard installed on the phone (todo #240): VAPID key, subscriptions, sending.

- VAPID key: ~/.config/ultron-hub/vapid.key (0600, created once, next to close.key), never printed nor logged. Only the
  public half leaves this module (`public_key`), as the page's applicationServerKey.
- Subscriptions: one per allowed device (the dashboard's whois name), in ~/.local/state/ultron-hub/dashboard-push/
  (folder 0700, file 0600). An endpoint is a capability URL: never logged, only its host. Endpoints are restricted to the
  known push services (no request to an arbitrary URL).
- `notify(text, ...)`: called by `envoi.envoyer` AFTER Telegram; never raises, a push failure never touches Telegram.
  The text is reduced to a short, cleaned summary (no MEDIA lines, no paths, no token-like strings).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import pwd
import re
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from ultron_hub.dashboard import pushcrypto as pc

log = logging.getLogger("ultron_hub.dashboard.push")

_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)  # from pwd, not $HOME (same rule as close.key)
KEY_PATH = _HOME / ".config" / "ultron-hub" / "vapid.key"
STORE_DIR = _HOME / ".local" / "state" / "ultron-hub" / "dashboard-push"
STORE_FILE = "abonnements.json"
PUSH_HOSTS = ("push.apple.com", "fcm.googleapis.com", "push.services.mozilla.com", "notify.windows.com")
ENDPOINT_MAX = 1024
TIMEOUT_S = 5
TTL_S = 24 * 3600
JWT_LIFE_S = 12 * 3600
TITLE_MAX, BODY_MAX = 60, 180
TABS = ("briefing", "travail", "equipes", "moi")  # todo #270 (were accueil, todos, escouades, questions)


class PushError(Exception):
    pass


# --- VAPID key --------------------------------------------------------------------------------------------------------
def _private(path: Path | None = None) -> int:
    """The VAPID private scalar; created once (0600, O_EXCL, no symlink follow)."""
    path = path or KEY_PATH
    try:
        raw = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        d = pc.new_private()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:  # created by another process meanwhile
            return _private(path)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(pc.b64u(d.to_bytes(32, "big")) + "\n")
        return d
    d = int.from_bytes(pc.unb64u(raw), "big")
    if not 0 < d < pc.N:
        raise PushError("vapid key unreadable")
    if path.stat().st_mode & 0o077:
        log.warning("vapid key readable by others: chmod 600")
    return d


def public_key(path: Path | None = None) -> str:
    return pc.b64u(pc.public_of(_private(path)))


# --- Subscriptions ----------------------------------------------------------------------------------------------------
def check_endpoint(endpoint: str) -> str:
    """The endpoint if it is an https URL of a known push service, else PushError."""
    if not isinstance(endpoint, str) or not 0 < len(endpoint) <= ENDPOINT_MAX:
        raise PushError("endpoint refused")
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    if (parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443)
            or not any(host == h or host.endswith("." + h) for h in PUSH_HOSTS)):
        raise PushError("endpoint refused")
    return endpoint


def check_subscription(sub: object) -> dict:
    """{endpoint, p256dh, auth} from a PushSubscription.toJSON(), validated; PushError otherwise."""
    if not isinstance(sub, dict) or not isinstance(sub.get("keys"), dict):
        raise PushError("subscription refused")
    keys = sub["keys"]
    try:
        p256dh, auth = str(keys["p256dh"]), str(keys["auth"])
        pc.decode_point(pc.unb64u(p256dh))
        if len(pc.unb64u(auth)) != 16:
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise PushError("subscription refused") from None
    return {"endpoint": check_endpoint(sub.get("endpoint")), "p256dh": p256dh, "auth": auth}


@contextmanager
def _store(write: bool = False):
    """The subscriptions dict, under an exclusive lock; saved atomically (0600) when `write`."""
    STORE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = STORE_DIR / STORE_FILE
    with open(STORE_DIR / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        yield data
        if write:
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, path)


def subscribe(device: str, sub: object, origin_host: str) -> None:
    """Record (or replace) the device's subscription; `origin_host` = dashboard name, used as the VAPID `sub`."""
    row = check_subscription(sub)
    row["origine"] = origin_host.split(":")[0]
    row["depuis"] = int(time.time())
    with _store(write=True) as data:
        data[device] = row
    log.info("push subscribed device=%s service=%s", device, urlsplit(row["endpoint"]).hostname)


def unsubscribe(device: str) -> bool:
    with _store(write=True) as data:
        return data.pop(device, None) is not None


def subscribed(device: str) -> bool:
    with _store() as data:
        return device in data


def _record_sent(devices: list[str]) -> None:
    """Remember when each device last had a push accepted (`dernier_envoi`, epoch seconds)."""
    now = int(time.time())
    with _store(write=True) as data:
        for device in devices:
            if isinstance(data.get(device), dict):
                data[device]["dernier_envoi"] = now


def _forget_if_same(device: str, endpoint: str) -> None:
    with _store(write=True) as data:
        if (data.get(device) or {}).get("endpoint") == endpoint:
            data.pop(device)


# --- Text of a notification -------------------------------------------------------------------------------------------
_PATH = re.compile(r"(?<![\w])(?:~|/)[\w.\-]*(?:/[\w.\-]+)+/?")
_SECRETS = (re.compile(r"bot\d+:[A-Za-z0-9_-]+"), re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}"),
            re.compile(r"(?<![\w-])[A-Za-z0-9_\-+/=]{32,}"))


def _clean(line: str) -> str:
    line = _PATH.sub("…", line)
    for pattern in _SECRETS:
        line = pattern.sub("***", line)
    return " ".join(line.split())


def _cut(text: str, size: int) -> str:
    return text if len(text) <= size else text[:size - 1].rstrip() + "…"


def summary(text: str) -> tuple[str, str]:
    """(title, body): first line as title, a few next lines as body; MEDIA lines dropped, paths and secrets masked."""
    lines = [_clean(ln) for ln in str(text).splitlines() if not ln.strip().startswith("MEDIA:")]
    lines = [ln for ln in lines if ln]
    if not lines:
        return "Ultron", "Nouveau message"
    if len(lines) == 1:
        return "Ultron", _cut(lines[0], BODY_MAX)
    return _cut(lines[0], TITLE_MAX), _cut("\n".join(lines[1:4]), BODY_MAX)


def tab_for(text: str, sujet: str | None = None, chantier: str | None = None, boutons: object = None) -> str:
    """Which dashboard tab a click opens: a question (a decision of the briefing), a squad, a todo to act on, else home."""
    low = str(text).lower()
    if "question" in low:
        return "briefing"
    if chantier or "escouade" in low:
        return "equipes"
    if boutons or (sujet or "").lower() == "missions" or "fusion" in low or "todo" in low:
        return "travail"
    return "briefing"


# --- Sending ----------------------------------------------------------------------------------------------------------
def _urlopen(req: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - endpoint checked against PUSH_HOSTS


def _send_one(d: int, row: dict, payload: bytes) -> int:
    """HTTP status from the push service (0 = network failure)."""
    endpoint = check_endpoint(row["endpoint"])
    aud = "https://" + (urlsplit(endpoint).hostname or "")
    jwt = pc.vapid_jwt(d, aud, "https://" + str(row.get("origine") or "localhost"), int(time.time()) + JWT_LIFE_S)
    body = pc.encrypt(payload, pc.unb64u(row["p256dh"]), pc.unb64u(row["auth"]))
    req = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "TTL": str(TTL_S), "Urgency": "normal", "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "Authorization": f"vapid t={jwt}, k={pc.b64u(pc.public_of(d))}"})
    try:
        with _urlopen(req, TIMEOUT_S) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0


def notify(text: str, *, tab: str = "briefing", tag: str | None = None, only: str | None = None) -> int:
    """Push a cleaned summary of `text` to every subscribed device (or `only` that one); returns how many accepted it.
    Never raises."""
    try:
        with _store() as data:
            rows = {k: v for k, v in data.items() if only is None or k == only}
        if not rows:
            return 0
        title, body = summary(text)
        payload = json.dumps({"title": title, "body": body, "onglet": tab if tab in TABS else "briefing",
                              "tag": tag or ""}, ensure_ascii=False).encode()
        d = _private()
        sent = 0
        accepted: list[str] = []
        for device, row in rows.items():
            try:
                status = _send_one(d, row, payload)
            except Exception as exc:  # noqa: BLE001 - one bad row never stops the others
                log.warning("push failed device=%s: %s", device, type(exc).__name__)
                continue
            if 200 <= status < 300:
                sent += 1
                accepted.append(device)
                log.info("push accepted device=%s status=%d", device, status)  # never the endpoint nor keys
            elif status in (404, 410):  # subscription gone (app removed, permission withdrawn)
                _forget_if_same(device, row.get("endpoint"))
                log.info("push subscription expired device=%s status=%d: forgotten", device, status)
            else:
                log.warning("push refused device=%s status=%d", device, status)
        if accepted:
            _record_sent(accepted)
        return sent
    except Exception as exc:  # noqa: BLE001
        log.warning("push skipped: %s", type(exc).__name__)
        return 0
