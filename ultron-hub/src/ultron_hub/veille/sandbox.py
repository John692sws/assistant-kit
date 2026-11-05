"""Sandbox of the veilles (todo #87): a script only sees its own folder.

The script runs under bwrap (`repos.sandbox_argv`): system read-only, home, /run and /tmp empty, no network. Its
folder is put back read-only, only `travail/` is writable, and the `lecture` dirs of `veille.json` are readable.
Web pages come from `fetch_pages`, run by ultron-hub outside the sandbox (closed list of https URLs).
"""

import http.client
import ipaddress
import resource
import socket
import ssl
import time
import urllib.parse
from pathlib import Path
from typing import Any

from ultron_hub import repos
from ultron_hub.common.errors import InvalidRequestError

WORK = "travail"
LECTURE_MAX, URLS_MAX = 3, 3
URL_MAX_LEN = 300
PAGE_MAX = 1024 * 1024
PAGE_TIMEOUT_S = 15
MEMORY_MAX = 2 * 1024 ** 3
FILE_MAX = 16 * 1024 ** 2
FILES_OPEN_MAX = 256
BLOCKED_SUFFIXES = (".local", ".internal", ".localhost", ".ts.net", ".lan", ".home.arpa")


def lecture_roots() -> tuple[Path, ...]:
    return (Path.home() / "workspace" / "projects" / "active",)


def check_lecture(paths: Any) -> list[str]:
    if paths is None:
        return []
    if not isinstance(paths, list) or len(paths) > LECTURE_MAX:
        raise InvalidRequestError(f"lecture : liste de {LECTURE_MAX} dossiers au plus")
    clean = []
    for raw in paths:
        path = Path(raw) if isinstance(raw, str) else None
        if path is None or not path.is_absolute() or ".." in path.parts:
            raise InvalidRequestError("lecture : chemin absolu attendu")
        if path.resolve() != path or not path.is_dir():
            raise InvalidRequestError(f"lecture : {raw} n'est pas un dossier (ou passe par un lien)")
        if not any(root in path.parents for root in lecture_roots()):
            raise InvalidRequestError(f"lecture : seulement sous {lecture_roots()[0]}")
        if str(path) not in clean:
            clean.append(str(path))
    return clean


def _blocked_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return host == "localhost" or "." not in host or host.endswith(BLOCKED_SUFFIXES)


def check_url(url: Any) -> str:
    if not isinstance(url, str) or not url or len(url) > URL_MAX_LEN or any(c.isspace() for c in url):
        raise InvalidRequestError("url invalide")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.port:
        raise InvalidRequestError(f"url refusée (https, sans identifiant ni port) : {url}")
    if _blocked_host(parts.hostname):
        raise InvalidRequestError(f"url refusée (adresse locale ou écrite en chiffres) : {url}")
    return url


def check_urls(urls: Any) -> list[str]:
    if urls is None:
        return []
    if not isinstance(urls, list) or len(urls) > URLS_MAX:
        raise InvalidRequestError(f"urls : liste de {URLS_MAX} adresses au plus")
    clean = []
    for url in urls:
        if check_url(url) not in clean:
            clean.append(url)
    return clean


# Prefixes that embed an IPv4 address (NAT64, 6to4): never trusted, whatever `is_global` says.
EMBEDDED_V4 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"),
               ipaddress.ip_network("2002::/16"))


def _public_ip(host: str) -> str | None:
    """One public address of `host`, or None if any of its addresses is not public (or none resolves)."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return None
    addresses = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]
    if not addresses or not all(a.is_global and not any(a in net for net in EMBEDDED_V4 if a.version == 6)
                                for a in addresses):
        return None
    return str(addresses[0])


class _PinnedHTTPS(http.client.HTTPSConnection):
    """Connects to the address checked by `_public_ip` (no second DNS lookup), certificate checked for the name.
    http.client never reads the *_PROXY variables."""

    def __init__(self, host: str, ip: str, timeout: float):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, 443), timeout=self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _read_until(resp: http.client.HTTPResponse, deadline: float) -> bytes:
    chunks, size = [], 0
    while size <= PAGE_MAX:
        if time.monotonic() > deadline:
            raise TimeoutError("page trop lente")
        chunk = resp.read1(64 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def fetch_page(url: str) -> dict[str, Any]:
    """One page for a script; never raises (a page in error is reported to the script, which decides)."""
    try:
        check_url(url)
    except InvalidRequestError as exc:
        return {"url": url, "erreur": str(exc)}
    parts = urllib.parse.urlsplit(url)
    ip = _public_ip(parts.hostname)
    if ip is None:
        return {"url": url, "erreur": "adresse non publique ou introuvable"}
    deadline = time.monotonic() + PAGE_TIMEOUT_S
    conn = _PinnedHTTPS(parts.hostname, ip, PAGE_TIMEOUT_S)
    try:
        conn.request("GET", (parts.path or "/") + (f"?{parts.query}" if parts.query else ""),
                     headers={"User-Agent": "ultron-veille/1", "Accept-Encoding": "identity"})
        resp = conn.getresponse()
        if resp.status >= 300:
            return {"url": url, "erreur": f"réponse HTTP {resp.status}" + (" (redirection refusée)"
                                                                          if resp.status < 400 else "")}
        data = _read_until(resp, deadline)
        kind, charset = resp.headers.get_content_type(), resp.headers.get_content_charset() or "utf-8"
        status = resp.status
    except (OSError, http.client.HTTPException, ValueError) as exc:
        return {"url": url, "erreur": f"page injoignable ({type(exc).__name__})"}
    finally:
        conn.close()
    if len(data) > PAGE_MAX:
        return {"url": url, "erreur": f"page de plus de {PAGE_MAX // 1024} Kio"}
    try:
        text = data.decode(charset, "replace")
    except LookupError:
        text = data.decode("utf-8", "replace")
    return {"url": url, "status": status, "type": kind, "texte": text}


def fetch_pages(urls: list[str]) -> list[dict[str, Any]]:
    return [fetch_page(url) for url in urls]


def argv(script: Path, folder: Path, lecture: list[str], name: str) -> list[str]:
    work = folder / WORK
    cmd = repos.sandbox_argv([str(script)], work, [folder, *map(Path, lecture)])
    cut = cmd.index("--")
    return [*cmd[:cut], *_masks(lecture), "--setenv", "ULTRON_VEILLE", name, *cmd[cut:]]


def _masks(lecture: list[str]) -> list[str]:
    """A read-only repository keeps its code and .git, not its data (`var/`: todo base, logs) nor its .env files."""
    out: list[str] = []
    for raw in lecture:
        path = Path(raw)
        if (path / "var").is_dir() and not (path / "var").is_symlink():
            out += ["--tmpfs", str(path / "var")]
        for env in sorted(path.glob(".env*")):
            if env.is_dir() and not env.is_symlink():
                out += ["--tmpfs", str(env)]
            elif env.is_file() and not env.is_symlink():
                out += ["--ro-bind", "/dev/null", str(env)]
    return out


def limits(timeout: int):
    """preexec_fn of the bwrap process: the limits are inherited by the script."""
    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_MAX, MEMORY_MAX))
        resource.setrlimit(resource.RLIMIT_CPU, (timeout + 5, timeout + 5))
        resource.setrlimit(resource.RLIMIT_FSIZE, (FILE_MAX, FILE_MAX))
        resource.setrlimit(resource.RLIMIT_NOFILE, (FILES_OPEN_MAX, FILES_OPEN_MAX))
    return apply
