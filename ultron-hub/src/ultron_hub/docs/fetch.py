"""Download a link for /docs, safely (todo #48).

http/https only, ports 80/443 only, no credentials in the URL. The host is resolved ONCE and every address must be
public (no private, loopback, link-local, multicast or reserved range); the connection then goes to that address,
so a second DNS answer cannot redirect it. Each redirect (3 at most) goes through the same checks.
Timeout 20 s for the whole download, 5 MB at most.
"""

import http.client
import ipaddress
import socket
import ssl
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

from ultron_hub.docs.errors import DocsError

TIMEOUT_S = 20
MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
MAX_URL_CHARS = 2000
USER_AGENT = "ultron-hub-docs/1"


def resolve(host: str, port: int) -> list[str]:
    """Addresses of the host (a seam for the tests: they never touch the network)."""
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except OSError:
        raise DocsError("Je ne trouve pas ce site.") from None


def is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_url(url: Any) -> tuple[str, str, int, str]:
    """(scheme, host, port, path+query) of an acceptable URL, else DocsError."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS or any(c.isspace() or ord(c) < 32 for c in url):
        raise DocsError("Ce lien n'est pas valide.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise DocsError("Ce lien n'est pas valide.") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise DocsError("Seuls les liens http et https sont acceptés.")
    if parts.username is not None or parts.password is not None:
        raise DocsError("Les liens avec identifiants ne sont pas acceptés.")
    port = port or (443 if parts.scheme == "https" else 80)
    if port not in (80, 443):
        raise DocsError("Ce port n'est pas accepté.")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return parts.scheme, parts.hostname, port, path


def safe_address(host: str, port: int) -> str:
    addresses = resolve(host, port)
    if not addresses or not all(is_public(a) for a in addresses):
        raise DocsError("Ce lien mène vers une adresse privée ou locale : refusé.")
    return addresses[0]


def request(scheme: str, host: str, address: str, port: int, path: str, deadline: float) -> tuple[int, dict[str, str], bytes]:
    """One GET to a checked address (a seam for the tests). The body is read up to MAX_BYTES + 1."""
    remaining = max(1.0, deadline - time.monotonic())
    if scheme == "https":
        conn: http.client.HTTPConnection = _PinnedHTTPS(host, address, port, remaining)
    else:
        conn = _PinnedHTTP(host, address, port, remaining)
    try:
        conn.request("GET", path, headers={"Host": host if port in (80, 443) else f"{host}:{port}",
                                           "User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity",
                                           "Connection": "close"})
        resp = conn.getresponse()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        body = b""
        if resp.status not in (301, 302, 303, 307, 308):
            chunks, size = [], 0
            while size <= MAX_BYTES:
                if time.monotonic() > deadline:
                    raise DocsError("Le site met trop de temps à répondre.")
                chunk = resp.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            body = b"".join(chunks)[:MAX_BYTES + 1]
        return resp.status, headers, body
    except DocsError:
        raise
    except (OSError, http.client.HTTPException):
        raise DocsError("Je n'arrive pas à télécharger ce lien.") from None
    finally:
        conn.close()


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, address: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # certificate checked for the name


def fetch(url: str) -> tuple[str, bytes]:
    """(content type, body) of the link, after at most 3 checked redirects."""
    deadline = time.monotonic() + TIMEOUT_S
    for _ in range(MAX_REDIRECTS + 1):
        scheme, host, port, path = check_url(url)
        address = safe_address(host, port)
        status, headers, body = request(scheme, host, address, port, path, deadline)
        if status in (301, 302, 303, 307, 308):
            location = headers.get("location")
            if not location:
                raise DocsError("Redirection sans destination.")
            url = urljoin(url, location)
            continue
        if status != 200:
            raise DocsError(f"Le site a répondu {status}.")
        if len(body) > MAX_BYTES:
            raise DocsError("Ce lien est trop gros (5 Mo au maximum).")
        return headers.get("content-type", "").split(";")[0].strip().lower(), body
    raise DocsError("Trop de redirections (3 au maximum).")
