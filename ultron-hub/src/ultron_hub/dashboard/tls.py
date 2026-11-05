"""HTTPS of the dashboard (todo #239): the Tailscale certificate of the server, served by the dashboard itself.

`fetch` gets (or renews) it with `tailscale cert` into ~/.local/state/ultron-hub/dashboard-tls/ (folder 0700, files 0600),
outside the repository, and says whether it changed. `context` loads it for the server; None = plain HTTP (fallback).
Not `tailscale serve`: a local proxy would hide the calling device from `tailscale whois`.
"""

import hashlib
import os
import ssl
import subprocess
from pathlib import Path
from typing import Any

from ultron_hub.dashboard import auth

DEFAULT_DIR = Path("~/.local/state/ultron-hub/dashboard-tls")
CERT, KEY = "cert.pem", "key.pem"
CERT_TIMEOUT_S = 120  # a renewal asks Let's Encrypt through tailscaled


class CertError(Exception):
    pass


def tls_dir(path: Path | None = None) -> Path:
    return (path or DEFAULT_DIR).expanduser()


def cert_domain() -> str:
    """The server's HTTPS name (CertDomains of `tailscale status --json`); empty = HTTPS not enabled on the tailnet."""
    domains = auth._run_json("status", "--json").get("CertDomains") or []
    for d in domains if isinstance(domains, list) else []:
        name = str(d).rstrip(".").lower()
        if auth.HOST_RE.fullmatch(name):
            return name
    raise CertError("pas de nom HTTPS dans tailscale status (HTTPS désactivé dans la console Tailscale ?)")


def _new_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _check_pair(cert: Path, key: Path) -> None:
    try:
        _new_context().load_cert_chain(cert, key)
    except (ssl.SSLError, OSError) as exc:
        raise CertError(f"certificat ou clé inutilisable : {type(exc).__name__}") from None


def _digest(*paths: Path) -> str:
    h = hashlib.sha256()
    for p in paths:
        h.update(p.read_bytes() if p.is_file() else b"")
        h.update(b"\0")
    return h.hexdigest()


def _expiry(cert: Path) -> str:
    try:  # best effort, for the summary only
        return str(ssl._ssl._test_decode_cert(str(cert)).get("notAfter") or "")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return ""


def _private_dir(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    if d.is_symlink() or not d.is_dir():
        raise CertError(f"{d} n'est pas un dossier")
    os.chmod(d, 0o700)


def fetch(domain: str, directory: Path | None = None) -> dict[str, Any]:
    """`tailscale cert` into <dir>/*.new, check the pair, then replace the files only if they changed."""
    d = tls_dir(directory)
    _private_dir(d)
    cert, key = d / CERT, d / KEY
    new_cert, new_key = d / (CERT + ".new"), d / (KEY + ".new")
    for p in (new_cert, new_key):
        p.unlink(missing_ok=True)
    try:
        proc = subprocess.run([auth.TAILSCALE, "cert", "--cert-file", str(new_cert), "--key-file", str(new_key), domain],
                              capture_output=True, text=True, timeout=CERT_TIMEOUT_S, check=False,
                              stdin=subprocess.DEVNULL, umask=0o077)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CertError(f"tailscale cert : {type(exc).__name__}") from None
    try:
        if proc.returncode != 0 or not new_cert.is_file() or not new_key.is_file():
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
            raise CertError(f"tailscale cert a échoué (code {proc.returncode}) {tail[0][:200]}".rstrip())
        for p in (new_cert, new_key):
            os.chmod(p, 0o600)
        _check_pair(new_cert, new_key)
        changed = _digest(new_cert, new_key) != _digest(cert, key)
        if changed:
            os.replace(new_key, key)
            os.replace(new_cert, cert)
    finally:
        for p in (new_cert, new_key):
            p.unlink(missing_ok=True)
    return {"changed": changed, "domain": domain, "expire": _expiry(cert)}


def context(directory: Path | None = None) -> tuple[ssl.SSLContext | None, str]:
    """(TLS context, '') or (None, why): no readable certificate = plain HTTP, as before todo #239."""
    d = tls_dir(directory)
    cert, key = d / CERT, d / KEY
    if not cert.is_file() or not key.is_file():
        return None, f"pas de certificat dans {d} (ultron-hub dashboard cert)"
    try:
        _check_pair(cert, key)
    except CertError as exc:
        return None, str(exc)
    ctx = _new_context()
    ctx.load_cert_chain(cert, key)
    mode = key.stat().st_mode & 0o077
    return ctx, (f"clé lisible par d'autres que le propriétaire ({oct(key.stat().st_mode & 0o777)})" if mode else "")
