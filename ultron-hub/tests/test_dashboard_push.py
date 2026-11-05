"""Web Push (todo #240): RFC 8291/8292 crypto in the standard library, VAPID key, subscriptions, sending through a fake
push service, the hook in `envoi.envoyer` (Telegram first, push never breaks it), and the dashboard endpoints. No network."""

import http.client
import json
import logging
import stat
import threading
import time
from pathlib import Path

import pytest

from ultron_hub import envoi
from ultron_hub.dashboard import actions, auth, push, server, views
from ultron_hub.dashboard import pushcrypto as pc

u = pc.unb64u
ROOT = Path(__file__).parents[1]
ENDPOINT = "https://web.push.apple.com/QGuQyavXutnMH6Yg"


# --- crypto -------------------------------------------------------------------------------------------------------------
def test_rfc8291_example_is_reproduced():
    body = pc.encrypt(u("V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24"),
                      u("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"),
                      u("BTBZMqHH6r4Tts7J_aSIgg"),
                      as_private=int.from_bytes(u("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"),
                      salt=u("DGv6ra1nlYgDCS1FRnbzlw"))
    assert pc.b64u(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3j"
        "l7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")


def test_aes_gcm_nist_vectors():
    assert pc.aes128gcm_encrypt(bytes(16), bytes(12), b"").hex() == "58e2fccefa7e3061367f1d57a4e7455a"
    assert pc.aes128gcm_encrypt(bytes(16), bytes(12), bytes(16)).hex() == (
        "0388dace60b6a392f328c2b971b2fe78ab6e47d42cec13bdf53a67b21257bddf")


def test_ecdsa_and_ecdh():
    a, b = pc.new_private(), pc.new_private()
    assert pc.ecdh(a, pc.public_of(b)) == pc.ecdh(b, pc.public_of(a))
    sig = pc.sign(a, b"msg")
    assert pc.verify(pc.public_of(a), b"msg", sig)
    assert not pc.verify(pc.public_of(a), b"other", sig) and not pc.verify(pc.public_of(b), b"msg", sig)
    bad = bytearray(pc.public_of(b))
    bad[-1] ^= 1
    with pytest.raises(ValueError):
        pc.ecdh(a, bytes(bad))  # a point off the curve is refused


# --- key and subscriptions ----------------------------------------------------------------------------------------------
def test_vapid_key_created_once_0600_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    k1 = push.public_key()
    assert stat.S_IMODE(push.KEY_PATH.stat().st_mode) == 0o600
    assert push.public_key() == k1 and len(u(k1)) == 65
    secret = push.KEY_PATH.read_text().strip()
    assert secret not in caplog.text and secret not in k1


def ua_keys():
    d = pc.new_private()
    return d, {"endpoint": ENDPOINT, "keys": {"p256dh": pc.b64u(pc.public_of(d)), "auth": pc.b64u(b"0123456789abcdef")}}


@pytest.mark.parametrize("endpoint", ["http://web.push.apple.com/x", "https://evil.example/x", "https://push.apple.com.evil.io/x",
                                      "https://u:p@web.push.apple.com/x", "https://web.push.apple.com:8443/x", "", None])
def test_endpoint_outside_push_services_refused(endpoint):
    _, sub = ua_keys()
    sub["endpoint"] = endpoint
    with pytest.raises(push.PushError):
        push.check_subscription(sub)


def test_bad_keys_refused():
    _, sub = ua_keys()
    with pytest.raises(push.PushError):
        push.check_subscription({**sub, "keys": {"p256dh": "AAAA", "auth": sub["keys"]["auth"]}})
    with pytest.raises(push.PushError):
        push.check_subscription({**sub, "keys": {"p256dh": sub["keys"]["p256dh"], "auth": "AAAA"}})


def test_subscribe_store_is_private_and_one_per_device():
    _, sub = ua_keys()
    push.subscribe("iphone", sub, "serveur-test.exemple.ts.net:8787")
    push.subscribe("iphone", sub, "serveur-test.exemple.ts.net:8787")
    path = push.STORE_DIR / push.STORE_FILE
    data = json.loads(path.read_text())
    assert list(data) == ["iphone"] and data["iphone"]["origine"] == "serveur-test.exemple.ts.net"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(push.STORE_DIR.stat().st_mode) == 0o700
    assert push.subscribed("iphone") and push.unsubscribe("iphone") and not push.subscribed("iphone")


# --- text ---------------------------------------------------------------------------------------------------------------
def test_summary_hides_paths_media_and_secrets():
    title, body = push.summary("✅ Mission #12 terminée\nRapport : /home/exemple/notes/x.md et ~/.config/ultron-hub/close.key\n"
                               "MEDIA:/tmp/a.png\njeton 123456789:AAHfjhsdkfjhsdkfjhskdjfhskdjfhskdj fin\n"
                               "clé " + "A" * 40 + "\nvoir /todo 3/4")
    text = title + "\n" + body
    assert title == "✅ Mission #12 terminée"
    for leak in ("/home/exemple", ".config", "close.key", "MEDIA", "a.png", "AAHfjh", "A" * 40):
        assert leak not in text, leak
    assert "/todo 3/4" not in body  # beyond the 3 body lines
    assert push.summary("") == ("Ultron", "Nouveau message")
    assert len(push.summary("x" * 1000)[1]) <= push.BODY_MAX


def test_tab_for():
    assert push.tab_for("❓ Nouvelle question") == "briefing"
    assert push.tab_for("bilan", chantier="agent-ultron") == "equipes"
    assert push.tab_for("Mission finie", sujet="missions") == "travail"
    assert push.tab_for("Disque à 91 %", sujet="alertes") == "briefing"


# --- sending through a fake push service -------------------------------------------------------------------------------
class FakeResp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_service(monkeypatch, status=201):
    seen = []

    def urlopen(req, timeout):
        seen.append(req)
        if isinstance(status, Exception):
            raise status
        if status >= 400:
            raise push.urllib.error.HTTPError(req.full_url, status, "x", {}, None)
        return FakeResp(status)
    monkeypatch.setattr(push, "_urlopen", urlopen)
    return seen


def decrypt(ua_d, auth_secret, ua_public, body):
    """User agent side of RFC 8291, with the same primitives (GCM in CTR mode is its own inverse)."""
    salt, idlen = body[:16], body[20]
    as_public, ct = body[21:21 + idlen], body[21 + idlen:]
    h = pc._hmac
    ikm = h(h(auth_secret, pc.ecdh(ua_d, as_public)), b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = h(salt, ikm)
    cek, nonce = h(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16], h(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    plain = pc.aes128gcm_encrypt(cek, nonce, ct[:-16])[:-16]
    assert pc.aes128gcm_encrypt(cek, nonce, plain)[-16:] == ct[-16:]  # tag checks
    assert plain.endswith(b"\x02")
    return json.loads(plain[:-1])


def test_notify_sends_encrypted_payload_with_vapid(monkeypatch):
    ua_d, sub = ua_keys()
    push.subscribe("iphone", sub, "serveur-test.exemple.ts.net:8787")
    seen = fake_service(monkeypatch)
    assert push.notify("Escouade finie\nbilan prêt dans /home/exemple/x", tab="equipes") == 1
    req = seen[0]
    assert req.full_url == ENDPOINT and req.get_method() == "POST"
    hdr = {k.lower(): v for k, v in req.header_items()}
    assert hdr["content-encoding"] == "aes128gcm" and hdr["ttl"] == str(push.TTL_S)
    t, k = hdr["authorization"].removeprefix("vapid t=").split(", k=")
    assert k == push.public_key()
    head, claims, sig = t.split(".")
    assert pc.verify(u(k), f"{head}.{claims}".encode(), u(sig))
    claims = json.loads(u(claims))
    assert claims["aud"] == "https://web.push.apple.com" and claims["sub"] == "https://serveur-test.exemple.ts.net"
    assert time.time() < claims["exp"] <= time.time() + 24 * 3600
    msg = decrypt(ua_d, b"0123456789abcdef", u(sub["keys"]["p256dh"]), req.data)
    assert msg == {"title": "Escouade finie", "body": "bilan prêt dans …", "onglet": "equipes", "tag": ""}


def test_gone_subscription_forgotten_other_errors_kept(monkeypatch):
    _, sub = ua_keys()
    push.subscribe("iphone", sub, "h")
    fake_service(monkeypatch, 500)
    assert push.notify("x") == 0 and push.subscribed("iphone")
    fake_service(monkeypatch, OSError("down"))
    assert push.notify("x") == 0 and push.subscribed("iphone")
    fake_service(monkeypatch, 410)
    assert push.notify("x") == 0 and not push.subscribed("iphone")


def test_accepted_send_is_logged_and_dated_without_secrets(monkeypatch, caplog):
    _, sub = ua_keys()
    push.subscribe("iphone", sub, "h")
    before = int(time.time())
    assert json.loads((push.STORE_DIR / push.STORE_FILE).read_text())["iphone"]["depuis"] >= before
    fake_service(monkeypatch, 201)
    with caplog.at_level(logging.INFO):
        assert push.notify("x") == 1
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "push accepted device=iphone status=201" in logged
    assert ENDPOINT not in logged and sub["keys"]["p256dh"] not in logged and sub["keys"]["auth"] not in logged
    row = json.loads((push.STORE_DIR / push.STORE_FILE).read_text())["iphone"]
    assert row["dernier_envoi"] >= before and row["depuis"] <= row["dernier_envoi"]
    fake_service(monkeypatch, 500)
    row["dernier_envoi"] = 1
    assert push.notify("x") == 0
    assert json.loads((push.STORE_DIR / push.STORE_FILE).read_text())["iphone"]["dernier_envoi"] >= before  # untouched


def test_no_subscription_no_call(no_real_push):
    assert push.notify("x") == 0 and no_real_push == []
    assert not push.KEY_PATH.exists()  # nothing to send: the key is not even created


# --- the common sending point ---------------------------------------------------------------------------------------------
def test_envoyer_pushes_after_telegram_and_push_never_breaks_it(monkeypatch):
    order = []
    monkeypatch.setattr(envoi, "_hermes", lambda *a, **k: order.append("telegram") or envoi.Resultat(envoyes=1))
    monkeypatch.setattr(push, "notify", lambda text, **k: order.append(("push", k["tab"])) or 1)
    assert envoi.envoyer("❓ question ouverte").ok
    assert order == ["telegram", ("push", "briefing")]

    def boom(*a, **k):
        raise RuntimeError("push down")
    monkeypatch.setattr(push, "notify", boom)
    assert envoi.envoyer("hello").ok  # Telegram result untouched

    sent = []
    monkeypatch.setattr(envoi, "_hermes", lambda *a, **k: envoi.Resultat("error", "hermes down"))
    monkeypatch.setattr(push, "notify", lambda text, **k: sent.append(text) or 1)
    assert envoi.envoyer("alerte").status == "error" and sent == ["alerte"]  # Telegram down: the phone still hears


# --- dashboard endpoints ----------------------------------------------------------------------------------------------------
PHONE_ID, SELF_ID = "nPHONE", "nSELF"
DEVICES = (auth.Device("telephone-test", PHONE_ID),)


@pytest.fixture
def dash():
    views.clear_cache()

    class D:
        node = PHONE_ID
    d = D()
    gate = auth.Gate(DEVICES, SELF_ID, "100.64.0.1", whois=lambda ip: d.node)
    srv = server.make_server("127.0.0.1", 0, gate, acts=actions.Actions(cli=["false"]))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    d.srv, d.host = srv, f"127.0.0.1:{srv.server_address[1]}"
    yield d
    srv.shutdown()
    srv.server_close()


def call(d, method, path, body=None, cookie=None):
    conn = http.client.HTTPConnection(d.host, timeout=10)
    headers = {"Host": d.host, "Origin": f"{d.srv.scheme}://{d.host}"}
    if cookie:
        headers["Cookie"] = f"{server.COOKIE}={cookie}"
    data = json.dumps(body).encode() if body is not None else None
    if data:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    r = conn.getresponse()
    raw, ctype, set_cookie = r.read(), r.getheader("Content-Type", ""), r.getheader("Set-Cookie")
    conn.close()
    return r.status, (json.loads(raw) if ctype.startswith("application/json") else raw), ctype, set_cookie


def sid(d):
    return call(d, "GET", "/")[3].split(";")[0].split("=", 1)[1]


def test_service_worker_served_and_push_info(dash):
    code, body, ctype, _ = call(dash, "GET", "/sw.js")
    assert code == 200 and ctype.startswith("text/javascript") and b"notificationclick" in body
    code, info, _, _ = call(dash, "GET", "/api/push")
    assert code == 200 and info["ok"] and info["cle"] == push.public_key() and info["abonne"] is False
    assert info["https"] is False


def test_subscribe_needs_https_session_and_valid_subscription(dash, monkeypatch):
    seen = fake_service(monkeypatch)
    _, sub = ua_keys()
    s = sid(dash)
    code, out, _, _ = call(dash, "POST", "/api/push/abonner", {"abonnement": sub}, cookie=s)
    assert code == 409 and not push.subscribed("telephone-test")  # plain HTTP: refused
    dash.srv.scheme = "https"  # the Origin check follows the scheme; TLS itself is tested in test_dashboard_tls
    code, _, _, _ = call(dash, "POST", "/api/push/abonner", {"abonnement": sub})
    assert code == 403 and not push.subscribed("telephone-test")  # no session cookie
    code, _, _, _ = call(dash, "POST", "/api/push/abonner", {"abonnement": {**sub, "endpoint": "https://evil.example/x"}},
                         cookie=s)
    assert code == 400 and not push.subscribed("telephone-test")
    code, out, _, _ = call(dash, "POST", "/api/push/abonner", {"abonnement": sub}, cookie=s)
    assert code == 200 and out == {"ok": True, "abonne": True} and push.subscribed("telephone-test")
    row = json.loads((push.STORE_DIR / push.STORE_FILE).read_text())["telephone-test"]
    assert row["origine"] == "127.0.0.1"
    for _ in range(50):  # the welcome notification goes out in a thread, to this device only
        if seen:
            break
        time.sleep(0.05)
    assert [r.full_url for r in seen] == [ENDPOINT]
    code, out, _, _ = call(dash, "POST", "/api/push/desabonner", {}, cookie=s)
    assert code == 200 and not push.subscribed("telephone-test")


def test_unknown_device_cannot_subscribe(dash):
    s = sid(dash)
    dash.node = "nSTRANGER"
    _, sub = ua_keys()
    code, _, _, _ = call(dash, "POST", "/api/push/abonner", {"abonnement": sub}, cookie=s)
    assert code == 403 and not push.subscribed("telephone-test")
    assert call(dash, "GET", "/api/push")[0] == 403


def test_page_registers_worker_and_after_merge_restarts_dashboard():
    js = (server.STATIC / "app.js").read_text(encoding="utf-8")
    sw = (server.STATIC / "sw.js").read_text(encoding="utf-8")
    assert 'serviceWorker.register("/sw.js")' in js and "/api/push/abonner" in js and "requestPermission" in js
    for event in ('"push"', '"notificationclick"', '"install"', '"fetch"'):
        assert event in sw, event
    assert "/api/" not in sw.split("var SHELL")[1].split(";")[0]  # API answers are never cached
    plan = json.loads((ROOT / "deploy" / "user" / "apres-fusion" / "todo-240.json").read_text())
    assert [(a["action"], a["args"]) for a in plan["actions"]] == [("user_unit_restart", ["ultron-hub-dashboard.service"])]
