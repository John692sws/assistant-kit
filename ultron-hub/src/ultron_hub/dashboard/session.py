"""The dashboard's session cookie (todo #267): a signed token, so a session survives a restart of the dashboard.

Cookie value = `v1.<device b64url>.<expiry>.<nonce>.<hmac>`, HMAC-SHA256 of `ultron-dashboard-session:v1:<device>:<expiry>:
<nonce>` with the dashboard's OWN key (`~/.config/ultron-hub/dashboard-session.key`, 0600, created once; never close.key,
which only signs proofs). Checked: signature (constant time), device == the device of THIS request (Tailscale whois, still
checked first by server.py), expiry. Nothing secret in the cookie; the key is never printed nor logged.
"""

import base64
import binascii
import hashlib
import hmac
import os
import pwd
import re
import secrets
import stat
from pathlib import Path

KEY_PATH = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config" / "ultron-hub" / "dashboard-session.key"  # from pwd, as close.key
KEY_BYTES = 32
VERSION = "v1"
TOKEN_RE = re.compile(r"v1\.[A-Za-z0-9_-]{1,200}\.[0-9]{1,12}\.[A-Za-z0-9_-]{16,64}\.[0-9a-f]{64}")


class SessionKeyError(Exception):
    pass


def load_key(path: Path | None = None) -> bytes:
    """The session key: created once (O_EXCL, 0600, no symlink); refused unless a regular file of ours, mode 0600."""
    path = path or KEY_PATH
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:  # created by another process meanwhile
            return load_key(path)
        key = secrets.token_hex(KEY_BYTES).encode()
        with os.fdopen(fd, "wb") as fh:
            fh.write(key + b"\n")
        return key
    except OSError as exc:
        raise SessionKeyError(f"clé de session illisible ({exc.strerror})") from None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise SessionKeyError("clé de session refusée : doit être un fichier à nous, en mode 0600")
        key = fh.read().strip()
    if len(key) < KEY_BYTES:
        raise SessionKeyError("clé de session trop courte")
    return key


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _unb64(text: str) -> str:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode()


def _mac(key: bytes, device: str, expiry: int, nonce: str) -> str:
    msg = f"ultron-dashboard-session:{VERSION}:{device}:{expiry}:{nonce}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def issue(key: bytes, device: str, expiry: int) -> str:
    nonce = secrets.token_urlsafe(16)
    return f"{VERSION}.{_b64(device)}.{expiry}.{nonce}.{_mac(key, device, expiry, nonce)}"


def valid(key: bytes, token: str | None, device: str, now: float) -> bool:
    if not token or not TOKEN_RE.fullmatch(token):
        return False
    _, dev, expiry, nonce, mac = token.split(".")
    try:
        dev = _unb64(dev)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    if not hmac.compare_digest(mac, _mac(key, dev, int(expiry), nonce)):
        return False
    return hmac.compare_digest(dev.encode(), device.encode()) and now < int(expiry)
