"""The dashboard server: standard library, bound to the Tailscale IP only.

Every request: allowed device (tailscale whois), then a known Host (no DNS rebinding). Reads are GET/HEAD; the only
POSTs are the generic steps of an action of the catalogue (actions.py, catalogue.py: never a route per action), which
also need the same Origin, a JSON body and the session cookie.
HTTPS (todo #239): the server itself speaks TLS with the Tailscale certificate (tls.py); without one, plain HTTP + warning.
Web Push (todo #240): /sw.js, GET /api/push (public key, subscribed?), POST /api/push/abonner|desabonner with the same
checks as an action; subscribing needs HTTPS.
The fil (todo #289): GET /api/pj/<name> serves an attachment kept in var/notifs/pj/, by stored name only (notifs.PJ_RE).
📎 joindre (todo #293): POST /api/fichier only STORES a file (recus.recevoir: var/recus, 0600, type from the content, size
caps), after the same checks as an action (device, Host, Origin, session cookie) and an octet-stream body; nothing is run.
Analysing it is the catalogue action `joindre` (sheet + confirmation), which receives the stored name only.
"""

import json
import re
import ssl
import threading
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from ultron_hub import notifs, recus
from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import load_settings
from ultron_hub.dashboard import actions, auth, catalogue, push, tls, views

STATIC = Path(__file__).parent / "static"
FILES = {"/": ("index.html", "text/html; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8"),
         "/app.js": ("app.js", "text/javascript; charset=utf-8"),
         "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
         "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"), "/icon-192.png": ("icon-192.png", "image/png"),
         "/icon-512.png": ("icon-512.png", "image/png"), "/sw.js": ("sw.js", "text/javascript; charset=utf-8")}
CHANTIER_RE = re.compile(r"/api/chantier/([a-z0-9-]{1,40})")
POSTS = {"/api/action/preparer": "prepare", "/api/action/confirmer": "confirm", "/api/action/lire": "read",
         "/api/push/abonner": "subscribe", "/api/push/desabonner": "unsubscribe"}
UPLOAD = "/api/fichier"  # todo #293: the one POST with a binary body (stored, never run)
COOKIE = "ultron_session"
HANDSHAKE_S = 10  # a TLS handshake that stalls only holds its own thread
BODY_MAX = 16384  # a 2000-character answer or notes, in UTF-8, plus the JSON around it
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
           "Referrer-Policy": "no-referrer",
           "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"}
log = get_logger("dashboard")


class Handler(BaseHTTPRequestHandler):
    server_version = "ultron-dashboard"
    sys_version = ""
    gate: auth.Gate  # set on the server class by make_server
    timeout = 60  # an idle connection does not hold a thread forever

    def _send(self, code: int, body: bytes, ctype: str, cookie: str | None = None, extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        for k, v in HEADERS.items():
            if k not in (extra or {}):  # only the html attachment reader overrides (todo #373)
                self.send_header(k, v)
        if cookie:  # Secure once served over HTTPS; plain HTTP (fallback) cannot carry a Secure cookie
            secure = "; Secure" if self.server.scheme == "https" else ""  # type: ignore[attr-defined]
            self.send_header("Set-Cookie", f"{COOKIE}={cookie}; Path=/; HttpOnly; SameSite=Strict{secure}")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj: Any) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _handle(self) -> None:
        device = self.server.gate.caller(self.client_address[0])  # type: ignore[attr-defined]
        if device is None:  # nothing else runs for an unknown caller
            log.warning("dashboard refused caller=%s", self.client_address[0])
            self._json(403, {"erreur": "appareil non autorisé"})
            return
        host = (self.headers.get("Host") or "").lower()
        if host not in self.server.hosts:  # type: ignore[attr-defined]
            log.warning("dashboard refused host device=%s", device)
            self._json(403, {"erreur": "adresse refusée"})
            return
        path = unquote(urlsplit(self.path).path)
        if self.command == "POST" and path == UPLOAD:
            self._upload(device, host)
            return
        if self.command == "POST" and path in POSTS:
            self._action(device, host, POSTS[path])
            return
        if self.command not in ("GET", "HEAD"):
            self._json(405, {"erreur": "méthode refusée"})
            return
        if path == "/":
            sid = self._session()
            fresh = None if self.server.actions.session_ok(sid, device) else self.server.actions.new_session(device)  # type: ignore[attr-defined]
            self._send(200, (STATIC / "index.html").read_bytes(), FILES["/"][1], cookie=fresh)
        elif path in FILES:
            name, ctype = FILES[path]
            self._send(200, (STATIC / name).read_bytes(), ctype)
        elif path.startswith(notifs.PJ_URL):
            self._pj(path[len(notifs.PJ_URL):])
        elif path == "/api/push":
            self._push_info(device)
        elif path == "/api/catalogue":  # what the page draws: ids, buttons, fields; no argv, no regex, no secret
            self._json(200, {"actions": catalogue.public()})
        elif path == "/api/state":
            self._json(200, views.state())
        elif path.startswith("/api/") and path[5:] in views.BLOCKS:
            self._json(200, views.block(path[5:]))
        elif CHANTIER_RE.fullmatch(path):
            try:
                self._json(200, {"ok": True, **views.fiche(path.rsplit("/", 1)[1])})
            except Exception as exc:  # noqa: BLE001
                self._json(200, {"ok": False, "erreur": type(exc).__name__})
        else:
            self._json(404, {"erreur": "introuvable"})

    def _pj(self, nom: str) -> None:
        """An attachment of the fil: only a stored name of var/notifs/pj/ (fullmatch). Images, text, audio (with Range) and html
        (sandboxed: `Content-Security-Policy: sandbox allow-scripts`, no allow-same-origin, so it never gets the dashboard origin)
        are inline (todo #373); the rest is downloaded."""
        file = notifs.pj_path(nom)
        try:
            body = file.read_bytes() if file else None
        except OSError:
            body = None
        if body is None:
            self._json(404, {"erreur": "introuvable"})
            return
        ctype = notifs.content_type(nom)
        kind = notifs.genre(nom)
        if kind == "fichier":
            self._send(200, body, ctype, extra={"Content-Disposition": f'attachment; filename="{nom}"'})
            return
        extra = {"Content-Disposition": f'inline; filename="{nom}"'}
        if kind == "html":
            extra |= {"Content-Security-Policy": "sandbox allow-scripts", "X-Frame-Options": "SAMEORIGIN"}
        code = 200
        if kind == "audio":
            extra["Accept-Ranges"] = "bytes"
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
            if m and (m.group(1) or m.group(2)):
                size = len(body)
                a, b = (int(m.group(1)), int(m.group(2) or size - 1)) if m.group(1) else (max(0, size - int(m.group(2))), size - 1)
                b = min(b, size - 1)
                if a > b or a >= size:
                    self._send(416, b"", ctype, extra={"Content-Range": f"bytes */{size}"})
                    return
                extra["Content-Range"] = f"bytes {a}-{b}/{size}"
                body, code = body[a:b + 1], 206
        self._send(code, body, ctype, extra=extra)

    def _session(self) -> str | None:
        try:
            morsel = SimpleCookie(self.headers.get("Cookie") or "").get(COOKIE)
        except CookieError:
            return None
        return morsel.value if morsel else None

    def _push_info(self, device: str) -> None:
        try:
            self._json(200, {"ok": True, "cle": push.public_key(), "abonne": push.subscribed(device),
                             "https": self.server.scheme == "https"})  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            log.warning("dashboard push info failed: %s", type(exc).__name__)
            self._json(200, {"ok": False, "erreur": "notifications indisponibles"})

    def _push(self, device: str, host: str, step: str, body: dict) -> None:
        if step == "unsubscribe":
            push.unsubscribe(device)
            self._json(200, {"ok": True, "abonne": False})
            return
        if self.server.scheme != "https":  # type: ignore[attr-defined]
            self._json(409, {"ok": False, "erreur": "notifications : le dashboard doit être servi en https"})
            return
        try:
            push.subscribe(device, body.get("abonnement"), host)
        except push.PushError:
            log.warning("dashboard refused push subscription device=%s", device)
            self._json(400, {"ok": False, "erreur": "abonnement refusé"})
            return
        threading.Thread(target=push.notify, args=("🔔 Notifications activées\nTu recevras ici les messages d'Ultron.",),
                         kwargs={"only": device}, daemon=True).start()
        self._json(200, {"ok": True, "abonne": True})

    def _same_origin(self, host: str) -> bool:
        return (self.headers.get("Origin") == f"{self.server.scheme}://{host}"  # type: ignore[attr-defined]
                and self.headers.get("Sec-Fetch-Site", "same-origin") == "same-origin")

    def _upload(self, device: str, host: str) -> None:
        """📎 joindre: Origin, octet-stream, length, session cookie, all BEFORE reading a byte; then stored by recus."""
        refus = None
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = -1
        if not self._same_origin(host):
            refus = (403, "origine refusée : rien n'a été gardé")
        elif (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/octet-stream":
            refus = (400, "requête refusée : rien n'a été gardé")
        elif not 0 < length <= recus.UPLOAD_MAX:
            refus = (413, f"fichier vide ou trop gros ({recus.UPLOAD_MAX // recus.MO} Mo au plus) : rien n'a été gardé")
        elif not self.server.actions.session_ok(self._session(), device):  # type: ignore[attr-defined]
            refus = (403, "session expirée : recharge la page, rien n'a été gardé")
        if refus:
            log.warning("dashboard refused upload device=%s: %s", device, refus[1])
            self.close_connection = True  # the unread body must never be parsed as a request
            self._json(refus[0], {"ok": False, "erreur": refus[1], **({"session": "renouveler"} if "session" in refus[1] else {})})
            return
        nom = unquote(self.headers.get("X-Nom-Fichier") or "")[:200]
        try:
            out = recus.recevoir(self.rfile, length, nom, self.server.recus_dir(),
                                vocal=self.headers.get("X-Source") == "vocal")  # type: ignore[attr-defined]
        except recus.Refuse as exc:
            log.warning("dashboard refused upload device=%s: %s", device, exc)
            self.close_connection = True
            self._json(409, {"ok": False, "erreur": str(exc)})
            return
        except OSError as exc:
            log.warning("dashboard upload failed device=%s: %s", device, type(exc).__name__)
            self.close_connection = True
            self._json(500, {"ok": False, "erreur": "enregistrement impossible : rien n'a été gardé"})
            return
        log.info("dashboard upload device=%s genre=%s octets=%d", device, out["genre"], out["taille"])
        self._json(200, {"ok": True, **out})

    def _action(self, device: str, host: str, step: str) -> None:
        """POST of an action (or a push subscription): same Origin, JSON body, session cookie of this device."""
        acts: actions.Actions = self.server.actions  # type: ignore[attr-defined]
        if not self._same_origin(host):
            log.warning("dashboard refused origin device=%s step=%s", device, step)
            self._json(403, {"ok": False, "erreur": "origine refusée : rien n'a été lancé"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = -1
        if not (self.headers.get("Content-Type") or "").startswith("application/json") or not 0 < length <= BODY_MAX:
            self._json(400, {"ok": False, "erreur": "requête refusée : rien n'a été lancé"})
            return
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            body = None
        if not isinstance(body, dict):
            self._json(400, {"ok": False, "erreur": "requête refusée : rien n'a été lancé"})
            return
        sid = self._session()
        if not acts.session_ok(sid, device):
            # the page renews it (GET /) and redoes `prepare` or the reading, never a confirmation (todo #267)
            self._json(403, {"ok": False, "session": "renouveler",
                             "erreur": "session expirée : recharge la page, rien n'a été lancé"})
            return
        if step in ("subscribe", "unsubscribe"):
            self._push(device, host, step, body)
            return
        try:
            out = getattr(acts, step)(device, sid, body)
        except actions.Refused as exc:
            log.warning("dashboard refused step=%s device=%s: %s", step, device, exc)
            self._json(409, {"ok": False, "erreur": str(exc)})
            return
        self._json(200, {"ok": True, **out} if step == "prepare" else out)

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _handle

    def log_message(self, fmt: str, *args: Any) -> None:  # no query string, no body: path and status only
        log.info("dashboard %s", fmt % args if args else fmt)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr: tuple[str, int], gate: auth.Gate, names: tuple[str, ...] = (),
                 acts: actions.Actions | None = None, tls_ctx: ssl.SSLContext | None = None):
        self.gate = gate
        self.actions = acts or actions.Actions()
        super().__init__(addr, Handler)
        self.scheme = "https" if tls_ctx else "http"
        if tls_ctx:  # handshake in the request's thread (finish_request), never in the accept loop
            self.socket = tls_ctx.wrap_socket(self.socket, server_side=True, do_handshake_on_connect=False)
        port = self.server_address[1]
        self.recus_dir = lambda: recus.dossier(load_settings())  # where 📎 joindre stores (var/recus, as the CLI reads it)
        self.hosts = frozenset(f"{n}:{port}" for n in (self.server_address[0], *names))  # Host and Origin allowed

    def finish_request(self, request: Any, client_address: Any) -> None:
        if isinstance(request, ssl.SSLSocket):
            try:
                request.settimeout(HANDSHAKE_S)
                request.do_handshake()
            except (ssl.SSLError, OSError) as exc:  # plain HTTP on the TLS port, bad client, timeout: nothing served
                log.info("dashboard tls handshake failed caller=%s: %s", client_address[0], type(exc).__name__)
                return
        super().finish_request(request, client_address)


def make_server(ip: str, port: int, gate: auth.Gate, names: tuple[str, ...] = (),
                acts: actions.Actions | None = None, tls_ctx: ssl.SSLContext | None = None) -> Server:
    return Server((ip, port), gate, names, acts, tls_ctx)


def serve(port: int, config_path: Path, tls_dir: Path | None = None) -> None:
    devices = auth.load_devices(config_path)
    ip = auth.tailscale_ip()
    self_id, names = auth.self_status()
    ctx, why = tls.context(tls_dir)
    if ctx is None:
        log.warning("dashboard TLS off, plain HTTP: %s", why)
    elif why:
        log.warning("dashboard TLS: %s", why)
    server = make_server(ip, port, auth.Gate(devices, self_id, ip), names, tls_ctx=ctx)
    log.info("dashboard listening on %s://%s:%d for %d device(s)", server.scheme, names[0] if names and ctx else ip, port,
             len(devices))
    try:
        server.serve_forever()
    finally:
        server.server_close()
